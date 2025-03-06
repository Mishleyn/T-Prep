from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from fastapi.middleware.cors import CORSMiddleware
import pytesseract
from PIL import Image
from openai import OpenAI
import os
from dotenv import load_dotenv
from docx import Document

from models import Base, User, Question, Answer, ReviewSchedule
from schemas import UserCreate, UserLogin, QuestionCreate, AnswerCreate
from crud import (
    get_user_by_email, create_user, create_question, create_answer,
    get_questions_by_user, schedule_review
)
from database import SessionLocal, engine
from utils import get_password_hash, verify_password, create_access_token
from tasks import schedule_notification

#Настройки
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENAI_API_KEY")
)

app = FastAPI()

# Создание таблиц в БД
Base.metadata.create_all(bind=engine)


origins = ['*']

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*']
)


# Зависимость для получения сессии БД
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Регистрация пользователя
@app.post("/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    db_user = get_user_by_email(db, email=user.email)
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    hashed_password = get_password_hash(user.password)
    db_user = create_user(db, user=user)
    return {"message": "User created successfully", "success": True}


# Аутентификация
@app.post("/token")
def login(user: UserLogin, db: Session = Depends(get_db)):
    db_user = get_user_by_email(db, email=user.email)
    if not db_user or not verify_password(user.password, db_user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    access_token = create_access_token(data={"sub": db_user.email})
    return {"access_token": access_token, "token_type": "bearer", "success": True}


# Импорт вопросов из файла
@app.post("/import-questions")
def import_questions(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if file.filename.endswith(".txt"):
        content = file.file.read().decode("utf-8")
        questions = [line.strip() for line in content.split("\n") if line.strip()]
    elif file.filename.endswith(".docx"):
        doc = Document(file.file)
        questions = [para.text for para in doc.paragraphs if para.text.strip()]
    else:
        raise HTTPException(status_code=400, detail="Unsupported file format")

    for question_text in questions:
        create_question(db, question=QuestionCreate(question_text=question_text), user_id=1)  # Нужно передавать ID текущего пользователя
    return {"message": f"Imported {len(questions)} questions"}


@app.post("/generate-answer")
def generate_answer(question_id: int, db: Session = Depends(get_db)):
    question = db.query(Question).filter(Question.id == question_id).first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    response = client.chat.completions.create(
        model="deepseek/deepseek-chat:free",
        messages=[
            {
                "role": "user",
                "content": f"{question.question_text}. Ответ пиши <= 50 слов. Пиши строго по теме."

            }
        ]
    )
    answer_text = response.choices[0].message.content
    create_answer(db, answer=AnswerCreate(answer_text=answer_text), question_id=question_id)
    return {"answer": answer_text}


# Распознавание текста с изображения
@app.post("/ocr")
def ocr(image: UploadFile = File(...)):
    img = Image.open(image.file)
    text = pytesseract.image_to_string(img)
    return {"text": text}


# Логика интервальных повторений
@app.post("/start-review")
def start_review(question_id: int, db: Session = Depends(get_db)):
    intervals = [20 * 60, 8 * 3600, 24 * 3600]  # В секундах
    for interval in intervals:
        schedule_notification.apply_async(
            (question_id, interval), countdown=interval
        )
    return {"message": "Review scheduled"}


@app.get("/get-questions")
def get_questions(db: Session = Depends(get_db)):
    questions = db.query(Question).all()
    response = []
    for question in questions:
        temp = {}
        temp["id"] = question.id
        temp["text"] = question.question_text
        answer = db.query(Answer).filter(Answer.question_id == question.id).first()
        if answer is not None:
            temp["answer"] = answer.answer_text
        else:
            temp["answer"] = None
        response.append(temp)
    return response


load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")  # Ensure this is set correctly

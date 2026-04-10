import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-change-me')
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Gemma AI
    GOOGLE_AI_API_KEY = os.getenv('GOOGLE_AI_API_KEY')
    GEMMA_MODEL = os.getenv('GEMMA_MODEL', 'gemma-4-27b-it')

    # SMTP
    SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
    SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
    SMTP_USERNAME = os.getenv('SMTP_USERNAME')
    SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')
    SENDER_NAME = os.getenv('SENDER_NAME', 'Super Lawyer Reachout')

    # Optional
    NEWS_API_KEY = os.getenv('NEWS_API_KEY')
    HUNTER_API_KEY = os.getenv('HUNTER_API_KEY')

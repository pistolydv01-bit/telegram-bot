import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from google import genai

# Telegram Bot Token
TELEGRAM_BOT_TOKEN = "8774660739:AAEiQRS-b7inYvsH7i5EO0eRD5reoCu74ek"

# Google Gemini API Key (आपकी दी हुई असली की)
GEMINI_API_KEY = "AQ.Ab8RN6KSiymrWh8X9Mci2QA6dUWSoYQlxbfkPO8Z9s3nkiGeBg"

# नए और सही तरीके से क्लाइंट इनिशियलाइज किया है
client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# ReportLab से कलरफुल पीडीएफ बनाने का फंक्शन
def generate_pdf(text_content, filename="questions_output.pdf"):
    doc = SimpleDocTemplate(filename, pagesize=letter,
                            rightMargin=40, leftMargin=40,
                            topMargin=40, bottomMargin=40)
    story = []
    
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'TitleStyle',
        parent=styles['Heading1'],
        fontSize=20,
        textColor=colors.HexColor('#1a73e8'),
        alignment=1,
        spaceAfter=20
    )
    
    body_style = ParagraphStyle(
        'BodyStyle',
        parent=styles['Normal'],
        fontSize=11,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=12,
        leading=16
    )

    story.append(Paragraph("Question Bank & Notes", title_style))
    story.append(Spacer(1, 10))

    for line in text_content.split('\n'):
        if line.strip():
            story.append(Paragraph(line, body_style))

    doc.build(story)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    await update.message.reply_text("⏳ जेमिनी (Gemini) आपके सवालों को व्यवस्थित कर रहा है, पीडीएफ तैयार हो रही है...")

    try:
        prompt = f"Format the given raw questions cleanly, line by line, so they are ready for a document:\n\n{user_text}"
        
        # जेमिनी से रिस्पॉन्स लेने का सही तरीका
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        ai_output = response.text

        pdf_filename = "questions_output.pdf"
        generate_pdf(ai_output, pdf_filename)

        await update.message.reply_document(
            document=open(pdf_filename, "rb"), 
            caption="लो आपका शानदार पीडीएफ तैयार है! 📄✨"
        )

        if os.path.exists(pdf_filename):
            os.remove(pdf_filename)

    except Exception as e:
        await update.message.reply_text(f"❌ कुछ एरर आ गया है: {str(e)}")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    
    print("🤖 बॉट शुरू हो गया है...")
    app.run_polling()

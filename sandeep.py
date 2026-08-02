import os
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# नया और सही Telegram Bot Token और OpenAI API Key
TELEGRAM_BOT_TOKEN = "8774660739:AAEiQRS-b7inYvsH7i5EO0eRD5reoCu74ek"
OPENAI_API_KEY = "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"

client = OpenAI(api_key=OPENAI_API_KEY)

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
    await update.message.reply_text("⏳ चैटजीपीटी आपके सवालों को व्यवस्थित कर रहा है, पीडीएफ तैयार हो रही है...")

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful assistant. Format the given raw questions cleanly, line by line, so they are ready for a document."},
                {"role": "user", "content": user_text}
            ]
        )
        ai_output = response.choices[0].message.content

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

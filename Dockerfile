FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy.py .
COPY templates/ ./templates/

EXPOSE 8800

CMD ["python", "proxy.py"]
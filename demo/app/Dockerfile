FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "flagsmith>=6.3.0,<7"
COPY app.py .
EXPOSE 8080
CMD ["python", "-u", "app.py"]

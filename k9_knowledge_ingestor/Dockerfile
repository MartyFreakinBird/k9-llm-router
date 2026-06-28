FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc g++ && apt-get clean
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY k9_knowledge_ingestor.py .
CMD ["python", "k9_knowledge_ingestor.py"]

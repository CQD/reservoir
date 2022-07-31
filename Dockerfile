FROM python:3.10-slim

RUN mkdir -p /app
WORKDIR /app

COPY ./requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt

ENV ENVIRONMENT=local-dev
EXPOSE 80

CMD ["python", "/app/main.py"]
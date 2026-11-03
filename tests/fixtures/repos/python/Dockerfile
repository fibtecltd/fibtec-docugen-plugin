FROM python:3.12-slim
WORKDIR /srv
COPY app ./app
RUN pip install --no-cache-dir fastapi uvicorn sqlalchemy
EXPOSE 8000
CMD ["uvicorn", "app.api:router", "--host", "0.0.0.0"]

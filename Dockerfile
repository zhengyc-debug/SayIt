FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r "requirements.txt"

COPY app ./app/
COPY web ./web/
COPY tools ./tools/
COPY data ./data/
COPY main.py ./



EXPOSE 8787

CMD ["python","main.py", "--host", "0.0.0.0", "--port", "8787"]
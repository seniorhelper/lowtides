FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV HOST=0.0.0.0 PORT=8787 LOWTIDES_DB=/data/lowtides.db PYTHONUNBUFFERED=1
RUN mkdir -p /data
EXPOSE 8787
CMD ["python", "-m", "lowtides.server"]

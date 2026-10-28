FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV HOST=0.0.0.0 PORT=8787 COSTCOP_DB=/data/costcop.db PYTHONUNBUFFERED=1
RUN mkdir -p /data
EXPOSE 8787
CMD ["python", "-m", "costcop.server"]

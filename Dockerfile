FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    tk libtk8.6 tcl xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

RUN mkdir -p /app/data

COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 4295

CMD ["/start.sh"]
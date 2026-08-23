FROM python:3.10-slim

# Koyeb's log viewer always displays UTC and has no timezone setting of
# its own, and gunicorn's log lines use the container's system time (not
# Python's logging config) â€” so the only way to make *those* lines show
# IST is to set the container's own timezone. tzdata is what actually
# provides /usr/share/zoneinfo; python:3.10-slim doesn't include it by
# default, so it has to be installed explicitly.
ENV TZ=Asia/Kolkata
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . /app/

RUN pip install --no-cache-dir -r requirements.txt

RUN chmod +x start.sh

CMD ["./start.sh"]

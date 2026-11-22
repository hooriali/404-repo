# Mech Royale bot. Build from the repository root: docker build -t my-bot .
# Standard library only, so there is nothing to pip install and nothing to pin.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /bot
COPY mechfight.py navmap.py bot.py ./
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]

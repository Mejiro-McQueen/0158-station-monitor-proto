FROM python:3.13.1-alpine3.21

WORKDIR /usr/local/app

# Ask JPL MGSS for the monitor channel dictionary from AMPCS.
COPY monitor_channel.xml ./

# Install the application dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ./ ./

ENTRYPOINT ["python", "main.py"]
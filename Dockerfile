FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements_simple.txt .
RUN pip install --no-cache-dir -r requirements_simple.txt

# Copy the script
COPY price_monitor_simple.py .

# Run the monitor
CMD ["python", "-u", "price_monitor_simple.py"]

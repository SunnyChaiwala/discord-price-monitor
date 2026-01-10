FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements_web.txt .
RUN pip install --no-cache-dir -r requirements_web.txt

# Copy the script
COPY price_monitor_with_web.py .

# Expose port (Render will set PORT env var)
EXPOSE 10000

# Run the monitor with web server
CMD ["python", "-u", "price_monitor_with_web.py"]

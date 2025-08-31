FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the temp3 implementation (working starlink-grpc-tools)
COPY temp3/ ./temp3/

# Copy the main application
COPY src/ ./src/

# Set the working directory to src for running the app
WORKDIR /app/src

# Expose the Flask port
EXPOSE 5000

# Run the Flask application
CMD ["python", "app.py"]
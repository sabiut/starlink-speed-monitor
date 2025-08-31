FROM python:3.9-slim

WORKDIR /app

# Install system dependencies (including curl for healthcheck)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the temp3 implementation (working starlink-grpc-tools)
COPY temp3/ ./temp3/

# Copy the main application
COPY src/ ./src/

# Create directory for database and ensure permissions
RUN mkdir -p /app/data && chmod 755 /app/data

# Set the working directory to src for running the app
WORKDIR /app/src

# Expose the Flask port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:5000/health || exit 1

# Run the Flask application
CMD ["python", "app.py"]
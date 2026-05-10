FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.11

# Install deps first — Docker caches this layer until requirements change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY connectors/ connectors/
COPY db/ db/
COPY pipeline/ pipeline/
COPY lambdas/ lambdas/

RUN chmod -R 755 /var/task

CMD ["lambdas.s3_event_handler.handler"]

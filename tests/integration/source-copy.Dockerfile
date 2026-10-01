FROM alpine:3.20
RUN apk add --no-cache python3 openssh openssh-client sshpass rsync curl pv
WORKDIR /repo
ENTRYPOINT ["python3", "tests/integration/source_copy.py"]

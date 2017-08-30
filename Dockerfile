FROM alpine

RUN apk --update add py-pip build-base openssl-dev libffi-dev ca-certificates python-dev

COPY requirements.txt /usr/src/app/

RUN pip install -t /tmp -r /usr/src/app/requirements.txt

ADD handler.py /tmp/test.py

WORKDIR /tmp

CMD python /tmp/test.py

FROM python:3.8-slim-buster

ENV PYTHONUNBUFFERED=1
ENV PARADIGM_ENVIRONMENT="nightly"
ENV LOGGING_LEVEL="info"
ENV PARADIGM_MAKER_ACCOUNT_NAME="QAMakerTest"
ENV POST_ONLY='True'
ENV PARADIGM_MAKER_ACCESS_KEY="kI1ds50BaqwRIM1BlIWAVCyj"
ENV PARADIGM_MAKER_SECRET_KEY="1eJQiciEf/45yBo/muOcW2vSude7PJl6JbMxkzfgLad2t+/1"
ENV PARADIGM_BBO_ACCESS_KEY="DcWdBWu7w5d6YXZKEPUvTQKQ"
ENV PARADIGM_BBO_SECRET_KEY="U+ZbBqJ9KM9aRG+PjrduQR0aJYlG03MoyUliMB7HsptD9KOl"
ENV QUOTE_QUANTITY_LOWER_BOUNDARY="0"
ENV QUOTE_QUANTITY_HIGHER_BOUNDARY="5"
ENV QUOTE_PRICE_LOWER_BOUNDARY="100"
ENV QUOTE_PRICE_HIGHER_BOUNDARY="500"
ENV QUOTE_REFRESH_LOWER_BOUNDARY="1"
ENV QUOTE_REFRESH_HIGHER_BOUNDARY="5"

COPY requirements.txt /

RUN pip install -r /requirements.txt

COPY market-maker.py /

CMD ["sh", "-c", "python /market-maker.py $PARADIGM_ENVIRONMENT \
                                          $LOGGING_LEVEL \
                                          $PARADIGM_MAKER_ACCOUNT_NAME \
                                          $POST_ONLY \
                                          $PARADIGM_MAKER_ACCESS_KEY \
                                          $PARADIGM_MAKER_SECRET_KEY \
                                          $PARADIGM_BBO_ACCESS_KEY \
                                          $PARADIGM_BBO_SECRET_KEY \
                                          $QUOTE_QUANTITY_LOWER_BOUNDARY \
                                          $QUOTE_QUANTITY_HIGHER_BOUNDARY \
                                          $QUOTE_PRICE_LOWER_BOUNDARY \
                                          $QUOTE_PRICE_HIGHER_BOUNDARY \
                                          $QUOTE_REFRESH_LOWER_BOUNDARY \
                                          $QUOTE_REFRESH_HIGHER_BOUNDARY"]
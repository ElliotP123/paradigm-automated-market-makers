FROM python:3.8-slim-buster

ENV PYTHONUNBUFFERED=1
ENV PARADIGM_ENVIRONMENT="TEST"
ENV LOGGING_LEVEL="INFO"
ENV PARADIGM_MAKER_ACCOUNT_NAME="<venue-api-key-name-on-paradigm>"
ENV PARADIGM_MAKER_ACCESS_KEY="<access-key>"
ENV PARADIGM_MAKER_SECRET_KEY="<secret-key>"
ENV ORDER_NUMBER_PER_SIDE="5"
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
                                          $PARADIGM_MAKER_ACCESS_KEY \
                                          $PARADIGM_MAKER_SECRET_KEY \
                                          $PARADIGM_BBO_ACCESS_KEY \
                                          $PARADIGM_BBO_SECRET_KEY \
                                          $ORDER_NUMBER_PER_SIDE \
                                          $QUOTE_QUANTITY_LOWER_BOUNDARY \
                                          $QUOTE_QUANTITY_HIGHER_BOUNDARY \
                                          $QUOTE_PRICE_LOWER_BOUNDARY \
                                          $QUOTE_PRICE_HIGHER_BOUNDARY \
                                          $QUOTE_REFRESH_LOWER_BOUNDARY \
                                          $QUOTE_REFRESH_HIGHER_BOUNDARY"]
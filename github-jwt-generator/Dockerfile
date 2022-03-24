FROM ruby:latest

RUN gem install openssl jwt
COPY ./run.rb /run.rb

CMD ["ruby",  "/run.rb"]

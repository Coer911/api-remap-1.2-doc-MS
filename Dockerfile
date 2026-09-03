FROM ruby:2.5.1
# The base image already ships gem/bundler and mini_racer provides the JS runtime,
# so no extra apt packages are required. Debian stretch is archived and its packages
# no longer authenticate, which is why the old apt steps were dropped.
COPY . /usr/src/app
VOLUME /usr/src/app
EXPOSE 4567
WORKDIR /usr/src/app
RUN bundle install
CMD ["bundle", "exec", "middleman", "server", "--watcher-force-polling"]

# Desktop workspace (S1.4.2): Xvfb + a light desktop + Chromium, viewable and drivable from the
# app through the screen relay (S1.6.1, home_agents/screen_relay.py). Modelled on Anthropic's computer-use demo container.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:1 WIDTH=1280 HEIGHT=800
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb xfce4 xfce4-terminal x11vnc chromium-browser xdotool scrot \
      python3 python3-pip ca-certificates fonts-noto && \
    rm -rf /var/lib/apt/lists/* && useradd -m -u 10001 agent && \
    mkdir -p /run/screen && chown agent /run/screen
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh
USER agent
CMD ["/usr/local/bin/start.sh"]

# Desktop workspace (S1.4.2): Xvfb + a light desktop + Chromium, viewable through noVNC in the
# app's live view (S1.6.1). Modelled on Anthropic's computer-use demo container.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:1 WIDTH=1280 HEIGHT=800
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb xfce4 xfce4-terminal x11vnc novnc websockify chromium-browser xdotool scrot \
      python3 python3-pip ca-certificates fonts-noto && \
    rm -rf /var/lib/apt/lists/* && useradd -m -u 10001 agent
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh
USER agent
EXPOSE 6080
CMD ["/usr/local/bin/start.sh"]

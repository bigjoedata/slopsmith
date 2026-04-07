FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fluidsynth \
    fluid-soundfont-gm \
    libsndfile1 \
    curl \
    unzip \
    git \
    megatools \
    && rm -rf /var/lib/apt/lists/*

# Install vgmstream-cli
RUN curl -sL https://github.com/vgmstream/vgmstream/releases/download/r2083/vgmstream-linux-cli.zip -o /tmp/vgm.zip \
    && unzip -o /tmp/vgm.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/vgmstream-cli \
    && rm /tmp/vgm.zip

# Install .NET 10 SDK (for building RsCli)
RUN curl -sL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && chmod +x /tmp/dotnet-install.sh \
    && /tmp/dotnet-install.sh --channel 10.0 --install-dir /usr/share/dotnet \
    && ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet \
    && rm /tmp/dotnet-install.sh

ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1

# Build RsCli from Rocksmith2014.NET
RUN git clone --depth 1 https://github.com/iminashi/Rocksmith2014.NET.git /tmp/rs2014 \
    && mkdir -p /tmp/rs2014/tools/RsCli

COPY rscli/RsCli.fsproj /tmp/rs2014/tools/RsCli/
COPY rscli/Program.fs /tmp/rs2014/tools/RsCli/

# Disable NuGet audit warnings in the upstream project
RUN echo '<Project><PropertyGroup><NuGetAudit>false</NuGetAudit></PropertyGroup></Project>' > /tmp/rs2014/Directory.Build.props.override \
    && sed -i 's|</PropertyGroup>|<NuGetAudit>false</NuGetAudit></PropertyGroup>|' /tmp/rs2014/Directory.Build.props

RUN cd /tmp/rs2014/tools/RsCli \
    && dotnet publish -c Release -r linux-x64 --self-contained -o /opt/rscli \
    && rm -rf /tmp/rs2014 /root/.dotnet /root/.nuget

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code + bundled libraries
COPY lib/ /app/lib/
COPY static/ /app/static/
COPY plugins/ /app/plugins/
COPY server.py /app/

ENV PYTHONPATH=/app/lib:/app
ENV RSCLI_PATH=/opt/rscli/RsCli

EXPOSE 8000

CMD uvicorn server:app --host 0.0.0.0 --port 8000 --reload --reload-exclude "*.ogg" --reload-exclude "art/*"

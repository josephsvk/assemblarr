FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system assemblarr \
    && useradd --system --gid assemblarr --create-home --home-dir /home/assemblarr assemblarr

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-test.txt /app/

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN chown -R assemblarr:assemblarr /app

USER assemblarr

CMD ["sleep", "infinity"]

FROM runtime AS test

USER root

RUN python -m pip install --no-cache-dir -r /app/requirements-test.txt

USER assemblarr

CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM runtime AS audio-tools

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm sox \
    && npm install -g synaudio-cli \
    && node -e "const fs=require('fs'); const path='/usr/local/lib/node_modules/synaudio-cli/src/synaudio-cli.js'; const replacement=['const precisionScale = Math.max(0.1, Number(process.env.SYNAUDIO_PRECISION_SCALE ?? \"1\"));','const correlationSampleSize = Math.max(1, Math.round(sampleLength * commonSampleRate * precisionScale));','const initialGranularity = Number(process.env.SYNAUDIO_INITIAL_GRANULARITY ?? \"16\");','let synaudio = new SynAudio({ correlationSampleSize, initialGranularity, shared: false, });'].join('\\n'); let source=fs.readFileSync(path,'utf8'); const pattern=/let synaudio = new SynAudio\\(\\{\\s*correlationSampleSize:\\s*sampleLength \\* commonSampleRate,\\s*initialGranularity:\\s*16,\\s*shared:\\s*(?:true|false),\\s*\\}\\);/; if(!pattern.test(source)) throw new Error('Expected synaudio-cli source pattern not found'); fs.writeFileSync(path, source.replace(pattern, replacement));" \
    && sed -i 's/shared: true,/shared: false,/' /usr/local/lib/node_modules/synaudio-cli/src/synaudio-cli.js \
    && sed -i 's/^const simd=async()=>WebAssembly.validate.*/const simd=async()=>false/' /usr/local/lib/node_modules/synaudio-cli/node_modules/synaudio/src/SynAudio.js \
    && sed -i 's/return globalThis.Worker/return false \&\& globalThis.Worker/' /usr/local/lib/node_modules/synaudio-cli/node_modules/synaudio/src/SynAudio.js \
    && node -e "const fs=require('fs'); const path='/usr/local/lib/node_modules/synaudio-cli/node_modules/synaudio/src/SynAudio.js'; let source=fs.readFileSync(path,'utf8'); const pattern=/const promise = this\\._executeAsWorker\\(\\s*\"_runCorrelate\",\\s*syncParameters\\[currentIndex\\]\\[0\\],\\s*\\)/; if(!pattern.test(source)) throw new Error('Expected synaudio worker pattern not found'); fs.writeFileSync(path, source.replace(pattern, 'const promise = this._runCorrelate.apply(null, syncParameters[currentIndex][0])'));" \
    && rm -rf /var/lib/apt/lists/*

USER assemblarr

CMD ["sleep", "infinity"]

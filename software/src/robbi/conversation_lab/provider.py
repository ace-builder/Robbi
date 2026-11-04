"""Opt-in continuous conversation adapter; heavy speech models stay on the PC."""
import asyncio
import io
import json
import os
import queue
import time
import uuid
import wave

import httpx

from .engine import Conversation
from .models import Budget, ChatModels
from .settings import ContinuousSettings
from .voice_gate import VoiceGate
from .speech import segments


class Continuous:
    def __init__(self, runtime):
        self.r = runtime
        self.options = ContinuousSettings.load(runtime.config.root)
        self.engine = self.speech_http = self.model = None
        self.transcription = None
        self.synthesis = None
        self.last_activity = time.monotonic()
        self.input_epoch = 0

    async def run(self):
        from robbi.configuration.settings import settings
        key = os.environ.get('OPENROUTER_API_KEY') or getattr(settings, 'openrouter_api_key', '')
        ledger = self.r.config.root / 'var' / ('continuous-budget-' + uuid.uuid4().hex + '.json')
        budget = Budget(self.options.budget_usd, ledger)
        self.model = ChatModels(self.options, budget, key, self.r.event)
        token = os.environ.get('ROBBI_CONTINUOUS_SPEECH_KEY', '')
        self.speech_http = httpx.AsyncClient(base_url=self.options.speech_url.rstrip('/') + '/',
            timeout=20, trust_env=False, headers={'Authorization': 'Bearer ' + token} if token else {})
        speaker = None
        try:
            await self.model.start()
            if self.r.microphone:
                health = await self.speech_http.get('health', timeout=5)
                health.raise_for_status()
                if not health.json().get('ready'):
                    raise ValueError('PC speech service is not ready')
            self.engine = Conversation(self.model, self.r.executor, self.options, self.r.event)
            self.r.conversation = self.engine
            self.r.connected()
            self.r.event('status', 'Conversation experiment ready. Listening between replies; background jobs do not block conversation.')
            gate = VoiceGate(self.options.silence_ms) if self.r.microphone else None
            speaker = asyncio.create_task(self._speaker())
            await self._listen(gate)
        finally:
            if self.transcription:
                self.transcription.cancel()
                await asyncio.gather(self.transcription, return_exceptions=True)
            if speaker:
                speaker.cancel()
                await asyncio.gather(speaker, return_exceptions=True)
            if self.engine:
                await self.engine.close()
            await self.model.close()
            await self.speech_http.aclose()

    async def _accept(self, text):
        self.input_epoch += 1
        self.last_activity = time.monotonic()
        if self.synthesis and not self.synthesis.done():
            self.synthesis.cancel()
        audio = self.r.hardware.audio
        if audio:
            audio.interrupt()
        # A narrow exact-stop path remains immediate even when the model/network fails.
        if text.strip().lower().strip('.!') in ('stop', 'stop robbi', 'robbi stop', 'stop robot', 'cancel all', 'cancel everything'):
            history=getattr(self.r,'history',None)
            if history:history.append('user',text)
            if self.engine.foreground:
                self.engine.foreground.cancel()
            self.engine.generation += 1
            await self.engine.cancel_jobs()
            self.engine.offer('Stopped.', self.engine.generation)
        else:
            self.engine.submit(text)

    async def _listen(self, gate):
        while not self.r.cancelled.is_set():
            try:
                text = self.r.inputs.get_nowait()
            except queue.Empty:
                text = None
            if text:
                if self.transcription:
                    self.transcription.cancel()
                    await asyncio.gather(self.transcription, return_exceptions=True)
                    self.transcription = None
                if gate:
                    gate.reset()
                await self._accept(text)
            audio = self.r.hardware.audio
            if audio and gate:
                for _ in range(5):
                    try:
                        chunk = audio.input.get_nowait()
                    except queue.Empty:
                        break
                    if self.r.hardware.speaking():
                        gate.reset()
                        continue
                    pcm = gate.feed(chunk)
                    if gate.frames or pcm:
                        self.last_activity = time.monotonic()
                    if pcm:
                        if self.transcription and not self.transcription.done():
                            self.r.event('status', 'Speech recognition is busy; please repeat this last phrase')
                        else:
                            self.transcription = asyncio.create_task(self._transcribe(pcm))
            self.r.usage = {'budget_usd': self.options.budget_usd,
                'reserved_or_charged_usd': round(self.model.budget.used, 6)}
            await asyncio.sleep(.01)

    async def _transcribe(self, pcm):
        start = time.monotonic()
        out = io.BytesIO()
        with wave.open(out, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(pcm)
        try:
            response = await self.speech_http.post('v1/audio/transcriptions',
                files={'file': ('utterance.wav', out.getvalue(), 'audio/wav')},
                data={'language': self.options.language})
            response.raise_for_status()
            text = response.json().get('text', '')
            if not isinstance(text, str) or len(text) > 1000:
                raise ValueError('Invalid transcript')
            self.r.event('latency', json.dumps({'stage': 'stt', 'ms': round((time.monotonic() - start) * 1000)}))
            if text.strip() and not self.r.cancelled.is_set():
                self.r.event('you', text)
                await self._accept(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.r.event('error', f'Local speech recognition failed ({type(exc).__name__}); typed conversation still works')

    async def _speaker(self):
        while not self.r.cancelled.is_set():
            item = await self.engine.speech.get()
            if item.kind == 'ack':
                await asyncio.sleep(.6)  # A fast result replaces the unnecessary acknowledgement.
            while self.engine.valid(item) and (self.r.hardware.speaking()
                    or time.monotonic() - self.last_activity < .45
                    or (self.transcription and not self.transcription.done())):
                await asyncio.sleep(.04)
            if not self.engine.valid(item):
                continue
            self.synthesis = asyncio.create_task(self._render(item))
            try:
                await self.synthesis
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                if item.kind == 'result' and not item.emitted and self.engine.valid(item):
                    try:
                        self.engine.speech.put_nowait(item)
                    except asyncio.QueueFull:
                        pass

    async def _render(self, item):
        epoch = self.input_epoch
        audio = self.r.hardware.audio
        audio_epoch = getattr(audio, 'epoch', 0)
        def valid():
            return (self.engine.valid(item) and epoch == self.input_epoch
                and not self.r.cancelled.is_set() and getattr(audio, 'epoch', 0) == audio_epoch)
        async def enqueue(chunk=None, *, boundary=False):
            if not audio:return valid()
            # Backpressure covers both PCM and segment markers; no queue-full loss.
            while valid() and audio.output.qsize() >= 8:
                await asyncio.sleep(.02)
            if not valid():return False
            if boundary:
                segment = getattr(audio, 'segment', None)
                if segment:segment()
            else:audio.enqueue(chunk)
            return True
        try:
            start = time.monotonic()
            total = 0
            # Reuse one HTTP connection; synthesize the next sentence while V2
            # plays the preceding one. No new audio driver or protocol required.
            for sentence in segments(item.text):
                if not valid():break
                received = 0
                async with self.speech_http.stream('POST', 'v1/audio/speech', json={
                    'input': sentence, 'voice': self.options.voice, 'response_format': 'pcm'}) as response:
                    response.raise_for_status()
                    pending = b''
                    async for chunk in response.aiter_bytes(4800):
                        while not item.emitted and valid() and (
                                time.monotonic()-self.last_activity < .45
                                or (self.transcription and not self.transcription.done())):
                            await asyncio.sleep(.03)
                        if not valid():return
                        total += len(chunk);received += len(chunk)
                        if total > 24000 * 2 * 30:
                            raise ValueError('Speech exceeded 30 seconds')
                        if not item.emitted:
                            self.r.event('robbi', item.text)
                            self.r.event('latency', json.dumps({'stage': 'tts_first_pcm', 'ms': round((time.monotonic()-start)*1000)}))
                            item.emitted = True
                        pending += chunk
                        aligned = len(pending) // 2 * 2
                        if aligned and not await enqueue(pending[:aligned]):return
                        pending = pending[aligned:]
                    if pending or not received:raise ValueError('Incomplete or empty PCM speech')
                if not await enqueue(boundary=True):return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not item.emitted and self.engine.valid(item):
                self.r.event('robbi', item.text)
                item.emitted = True
            self.r.event('error', f'Speech output unavailable ({type(exc).__name__}); text remains visible')
        finally:
            if audio and epoch == self.input_epoch and getattr(audio, 'epoch', 0) == audio_epoch:
                while audio.output.qsize() >= 8 and not self.r.cancelled.is_set() and epoch == self.input_epoch:
                    await asyncio.sleep(.02)
                if epoch == self.input_epoch:audio.end_turn()

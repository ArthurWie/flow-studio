from kokoro import KPipeline
import soundfile as sf

pipeline = KPipeline(lang_code='a')  # 'a' = American English

text = "Hello! This is Kokoro running locally on my PC. Not robotic at all, right"

generator = pipeline(text, voice='af_heart')
for i, (gs, ps, audio) in enumerate(generator):
    sf.write(f'output_{i}.wav', audio, 24000)
    print(f'Saved output_{i}.wav')
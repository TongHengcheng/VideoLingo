import warnings
warnings.filterwarnings("ignore")
import os,sys
import whisperx
import torch
from typing import Dict
import librosa
from rich import print as rprint
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
import subprocess
import tempfile
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
from core.config_utils import load_key
from core.all_whisper_methods.demucs_vl import demucs_main
from core.all_whisper_methods.whisperXapi import process_transcription, convert_video_to_audio, split_audio, save_results, save_language
from core.all_whisper_methods.whisperXapi import RAW_AUDIO_FILE, BACKGROUND_AUDIO_FILE, VOCAL_AUDIO_FILE, AUDIO_DIR

MODEL_DIR = load_key("model_dir")

def transcribe_audio(audio_file: str, start: float, end: float) -> Dict:
    WHISPER_LANGUAGE = load_key("whisper.language")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    rprint(f"🚀 Starting WhisperX using device: {device} ...")
    
    if device == "cuda":
        gpu_mem = torch.cuda.get_device_properties(0).total_memory / (1024**3)
        batch_size = 16 if gpu_mem > 8 else 2
        compute_type = "float16" if torch.cuda.is_bf16_supported() else "int8"
        rprint(f"[cyan]🎮 GPU memory:[/cyan] {gpu_mem:.2f} GB, [cyan]📦 Batch size:[/cyan] {batch_size}, [cyan]⚙️ Compute type:[/cyan] {compute_type}")
    else:
        batch_size = 1
        compute_type = "int8"
        rprint(f"[cyan]📦 Batch size:[/cyan] {batch_size}, [cyan]⚙️ Compute type:[/cyan] {compute_type}")
    rprint(f"[green]▶️ Starting WhisperX for segment {start:.2f}s to {end:.2f}s...[/green]")
    
    try:
        if WHISPER_LANGUAGE == 'zh':
            model_name = "Huan69/Belle-whisper-large-v3-zh-punct-fasterwhisper"
            local_model = os.path.join(MODEL_DIR, "Belle-whisper-large-v3-zh-punct")
        else:
            model_name = "large-v3"
            local_model = os.path.join(MODEL_DIR, "large-v3")
            
        if os.path.exists(local_model):
            rprint(f"[green]📥 Loading local WHISPER model:[/green] {local_model} ...")
            model_name = local_model
        else:
            rprint(f"[green]📥 Using WHISPER model from HuggingFace:[/green] {model_name} ...")

        vad_options = {
                "vad_onset": 0.500,
                "vad_offset": 0.363
            }
        asr_options = {
                "temperatures": [0],
                "initial_prompt": "",
            }
        whisper_language = None if 'auto' in WHISPER_LANGUAGE else WHISPER_LANGUAGE
        model = whisperx.load_model(model_name, device, compute_type=compute_type, language=whisper_language, vad_options=vad_options, asr_options=asr_options, download_root=MODEL_DIR)

        # Create temporary file to store audio segment
        temp_audio = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
        temp_audio_path = temp_audio.name
        temp_audio.close()
        # Use ffmpeg to cut audio
        ffmpeg_cmd = f'ffmpeg -y -i "{audio_file}" -ss {start} -t {end-start} -vn -b:a 64k -ar 16000 -ac 1 -metadata encoding=UTF-8 -f mp3 "{temp_audio_path}"'
        subprocess.run(ffmpeg_cmd, shell=True, check=True, capture_output=True)
        # Load the cut audio
        audio_segment, sample_rate = librosa.load(temp_audio_path, sr=16000)
        # Delete temporary file
        os.unlink(temp_audio_path)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            transient=True
        ) as progress:
            task = progress.add_task("[cyan]Transcribing...", total=None)
            
            result = model.transcribe(audio_segment, batch_size=batch_size)
            progress.update(task, completed=True)

        # Free GPU resources
        del model
        torch.cuda.empty_cache()

        # Save language
        save_language(result['language'])
        if result['language'] == 'zh' and WHISPER_LANGUAGE != 'zh':
            raise ValueError("请指定转录语言为 zh 后重试！")

        # Align whisper output
        model_a, metadata = whisperx.load_align_model(language_code=result["language"], device=device)
        result = whisperx.align(result["segments"], model_a, metadata, audio_segment, device, return_char_alignments=False)

        # Free GPU resources again
        torch.cuda.empty_cache()
        del model_a

        # Adjust timestamps
        for segment in result['segments']:
            segment['start'] += start
            segment['end'] += start
            for word in segment['words']:
                if 'start' in word:
                    word['start'] += start
                if 'end' in word:
                    word['end'] += start
        return result
    except Exception as e:
        rprint(f"[red]WhisperX processing error:[/red] {e}")
        raise

def transcribe(video_file: str):
    if os.path.exists("output/log/cleaned_chunks.xlsx"):
        rprint("[yellow]⚠️ Transcription results already exist, skipping transcription step.[/yellow]")
        return
    
    audio_file = convert_video_to_audio(video_file)

    # step1 Demucs vocal separation
    if os.path.exists(os.path.join(AUDIO_DIR, BACKGROUND_AUDIO_FILE)):
        rprint(f"[yellow]⚠️ {os.path.join(AUDIO_DIR, BACKGROUND_AUDIO_FILE)} already exists, skip Demucs processing.[/yellow]")
    else:
        demucs_main(
            os.path.join(AUDIO_DIR, RAW_AUDIO_FILE),
            AUDIO_DIR,
            os.path.join(AUDIO_DIR, BACKGROUND_AUDIO_FILE),
            os.path.join(AUDIO_DIR, VOCAL_AUDIO_FILE)
        )
        print("Demucs processing completed, original_vocal.mp3 and background.mp3 saved")
    
    audio_file = os.path.join(AUDIO_DIR, VOCAL_AUDIO_FILE)

    # step2 Extract audio
    segments = split_audio(audio_file)
    
    # step3 Transcribe audio
    all_results = []
    for start, end in segments:
        result = transcribe_audio(audio_file, start, end)
        all_results.append(result)
    
    # step4 Combine results
    combined_result = {'segments': []}
    for result in all_results:
        combined_result['segments'].extend(result['segments'])
    
    df = process_transcription(combined_result)
    save_results(df)
        

if __name__ == "__main__":
    from core.step1_ytdlp import find_video_files
    video_file = find_video_files()
    rprint(f"[green]📁 Found video file:[/green] {video_file}, [green]starting transcription...[/green]")
    transcribe(video_file)

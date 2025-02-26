from typing import Dict, Optional, Union

import numpy as np
from .generation import codec_decode, generate_coarse, generate_fine, generate_text_semantic, SAMPLE_RATE
from .config import logger, console, console_file, get_default_values, load_all_defaults, VALID_HISTORY_PROMPT_DIRS
from scipy.io.wavfile import write as write_wav
from scipy.io import wavfile



import copy
## ADDED
import os
import re
import torch
import datetime
import random

import time
from bark_infinity import generation

from pathvalidate import sanitize_filename, sanitize_filepath

from rich.pretty import pprint
from rich.table import Table

from collections import defaultdict
from tqdm import tqdm

from bark_infinity import text_processing



from pydub import AudioSegment


global gradio_try_to_cancel
global done_cancelling




gradio_try_to_cancel = False
done_cancelling = False

def text_to_semantic(
    text: str,
    history_prompt: Optional[Union[Dict, str]] = None,
    temp: float = 0.7,
    silent: bool = False,
):
    """Generate semantic array from text.

    Args:
        text: text to be turned into audio
        history_prompt: history choice for audio cloning
        temp: generation temperature (1.0 more diverse, 0.0 more conservative)
        silent: disable progress bar

    Returns:
        numpy semantic array to be fed into `semantic_to_waveform`
    """


    x_semantic = generate_text_semantic(
        text,
        history_prompt=history_prompt,
        temp=temp,
        silent=silent,
        use_kv_caching=True
    )

    return x_semantic


def semantic_to_waveform(
    semantic_tokens: np.ndarray,
    history_prompt: Optional[Union[Dict, str]] = None,
    temp: float = 0.7,
    silent: bool = False,
    output_full: bool = False,
):
    """Generate audio array from semantic input.

    Args:
        semantic_tokens: semantic token output from `text_to_semantic`
        history_prompt: history choice for audio cloning
        temp: generation temperature (1.0 more diverse, 0.0 more conservative)
        silent: disable progress bar
        output_full: return full generation to be used as a history prompt

    Returns:
        numpy audio array at sample frequency 24khz
    """

    coarse_tokens = generate_coarse(
        semantic_tokens,
        history_prompt=history_prompt,
        temp=temp,
        silent=silent,
        use_kv_caching=True
    )
    bark_coarse_tokens = coarse_tokens

    fine_tokens = generate_fine(
        coarse_tokens,
        history_prompt=history_prompt,
        temp=0.5,
    )
    bark_fine_tokens = fine_tokens
    
    audio_arr = codec_decode(fine_tokens)
    if output_full:
        full_generation = {
            "semantic_prompt": semantic_tokens,
            "coarse_prompt": coarse_tokens,
            "fine_prompt": fine_tokens,
        }
        return full_generation, audio_arr
    return audio_arr


def save_as_prompt(filepath, full_generation):
    assert(filepath.endswith(".npz"))
    assert(isinstance(full_generation, dict))
    assert("semantic_prompt" in full_generation)
    assert("coarse_prompt" in full_generation)
    assert("fine_prompt" in full_generation)
    np.savez(filepath, **full_generation)


def generate_audio(
    text: str,
    history_prompt: Optional[Union[Dict, str]] = None,
    text_temp: float = 0.7,
    waveform_temp: float = 0.7,
    silent: bool = False,
    output_full: bool = False,
):
    """Generate audio array from input text.

    Args:
        text: text to be turned into audio
        history_prompt: history choice for audio cloning
        text_temp: generation temperature (1.0 more diverse, 0.0 more conservative)
        waveform_temp: generation temperature (1.0 more diverse, 0.0 more conservative)
        silent: disable progress bar
        output_full: return full generation to be used as a history prompt

    Returns:
        numpy audio array at sample frequency 24khz
    """
    semantic_tokens = text_to_semantic(
        text,
        history_prompt=history_prompt,
        temp=text_temp,
        silent=silent,
    )
    out = semantic_to_waveform(
        semantic_tokens,
        history_prompt=history_prompt,
        temp=waveform_temp,
        silent=silent,
        output_full=output_full,
    )
    if output_full:
        full_generation, audio_arr = out
        return full_generation, audio_arr
    else:
        audio_arr = out
    return audio_arr

## ADDED BELOW



def set_seed(seed: int = 0):
    """Set the seed
    seed = 0         Generate a random seed
    seed = -1        Disable deterministic algorithms
    0 < seed < 2**32 Set the seed
    Args:
        seed: integer to use as seed
    Returns:
        integer used as seed
    """

    original_seed = seed

    # See for more informations: https://pytorch.org/docs/stable/notes/randomness.html
    if seed == -1:
        # Disable deterministic

        print("Disabling deterministic algorithms")


        
        torch.backends.cudnn.deterministic = False
        torch.backends.cudnn.benchmark = True

        if "CUBLAS_WORKSPACE_CONFIG" in os.environ:
            del os.environ["CUBLAS_WORKSPACE_CONFIG"]

        torch.use_deterministic_algorithms(False) # not sure if needed, yes it is

    else:


        print("Enabling deterministic algorithms")

        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

        os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8" # not sure if this is needed, yes it is,
        torch.use_deterministic_algorithms(True)  # not sure if needed, yes it is

    if seed <= 0:
        # Generate random seed
        # Use default_rng() because it is independent of np.random.seed()
        seed = np.random.default_rng().integers(1, 2**32 - 1)

    assert(0 < seed and seed < 2**32)

    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)

    print(f"Set seed to {seed}")

    return original_seed if original_seed != 0 else seed

# mostly just looks in different directories and handles fuzzier matching like not including the extension
def process_history_prompt(user_history_prompt):

    valid_directories_to_check = VALID_HISTORY_PROMPT_DIRS
    
    if user_history_prompt is None:
        return None

    file_name, file_extension = os.path.splitext(user_history_prompt)
    if not file_extension:
        file_extension = '.npz'

    full_path = f"{file_name}{file_extension}"

    history_prompt_returned = None
    if os.path.dirname(full_path):  # Check if a directory is specified
        if os.path.exists(full_path):
            history_prompt_returned = full_path
        else:
            logger.error(f"  >> Can't find speaker file at: {full_path}")
    else:
        for directory in valid_directories_to_check:
            full_path_in_dir = os.path.join(directory, f"{file_name}{file_extension}")
            if os.path.exists(full_path_in_dir):
                history_prompt_returned = full_path_in_dir

    
    if history_prompt_returned is None:
        logger.error(f"  >>! Can't find speaker file: {full_path} in: {valid_directories_to_check}")
        return None
    
    if (not history_prompt_is_valid(history_prompt_returned)):
        logger.error(f"  >>! Speaker file: {history_prompt_returned} is invalid, skipping.")
        return None

    return history_prompt_returned

def log_params(log_filepath, **kwargs):


    from rich.console import Console
    file_console = Console(color_system=None)
    with file_console.capture() as capture:
        kwargs['history_prompt'] = kwargs.get('history_prompt_string',None)
        kwargs['history_prompt_string'] = None

        file_console.print(kwargs)
    str_output = capture.get()


    log_filepath = generate_unique_filepath(log_filepath)
    with open(log_filepath, "wt") as log_file:
        log_file.write(str_output)

    return


def determine_output_filename(special_one_off_path = None, **kwargs):
    if special_one_off_path: 
        return sanitize_filepath(special_one_off_path)

    # normally generate a filename
    output_dir = kwargs.get('output_dir',None)
    output_filename = kwargs.get('output_filename',None)


    # TODO: Offer a config for long clips to show only the original starting prompt. I prefer seeing each clip seperately names for easy referencing myself.
    text_prompt = kwargs.get('text_prompt',None) or kwargs.get('text',None) or ''
    history_prompt = kwargs.get('history_prompt_string',None) or 'random'
    text_prompt = text_prompt.strip()
    history_prompt = os.path.basename(history_prompt).replace('.npz', '')

    # There's a Lot of stuff that passes that sanitize check that we don't want in the filename
    text_prompt = re.sub(r' ', '_', text_prompt)  # spaces with underscores
    # quotes, colons, and semicolons
    text_prompt = re.sub(r'[^\w\s]|[:;\'"]', '', text_prompt)
    text_prompt = re.sub(r'[\U00010000-\U0010ffff]', '',
                         text_prompt, flags=re.UNICODE)  # Remove emojis
    segment_number_text = None
    hoarder_mode = kwargs.get('hoarder_mode', False)
    if hoarder_mode:
        segment_number = kwargs.get("segment_number")
        if segment_number and kwargs.get("total_segments", 1) > 1: 
            segment_number_text = f"{str(segment_number).zfill(3)}_"

    if output_filename is not None and output_filename.strip() != '':
        base_output_filename = f"{output_filename}"
    else:

        # makes the filename unique which is good when just browsing via search
        date_str = datetime.datetime.now().strftime("%y-%m%d-%H%M-%S")       

        truncated_text = re.sub(r'[^a-zA-Z0-9]', '', text_prompt) # this is brutal but I'm sick of weird filename problems.
        truncated_text = text_prompt[:15].strip()
        
        base_output_filename = f"{truncated_text}-{date_str}-SPK-{history_prompt}"

    if segment_number_text is not None:
        base_output_filename = f"{segment_number_text}{base_output_filename}"

    
    output_format = kwargs.get('output_format', None)

    if output_format is not None:
        if output_format in ['ogg', 'flac', 'mp4','wav']:
            base_output_filename = f"{base_output_filename}.{output_format}"
        else:
            base_output_filename = f"{base_output_filename}.mp3"


    output_filepath = (
        os.path.join(output_dir, base_output_filename))

    os.makedirs(output_dir, exist_ok=True)

    output_filepath = generate_unique_filepath(output_filepath)

    return output_filepath


def write_one_segment(audio_arr = None, full_generation = None, **kwargs):
    filepath = determine_output_filename(**kwargs)
    #print(f"Looks like filepath is {filepath} is okay?")
    if full_generation is not None:
        write_seg_npz(filepath, full_generation, **kwargs)
    if audio_arr is not None and kwargs.get("segment_number", 1) != "base_history":
        write_seg_wav(filepath, audio_arr, **kwargs)

    hoarder_mode = kwargs.get('hoarder_mode', False)
    dry_run = kwargs.get('dry_run', False)
    if hoarder_mode and not dry_run:
        log_params(f"{filepath}_info.txt",**kwargs)


def generate_unique_dirpath(dirpath):
    unique_dirpath = sanitize_filepath(dirpath)
    base_name = os.path.basename(dirpath)
    parent_dir = os.path.dirname(dirpath)
    counter = 1
    while os.path.exists(unique_dirpath):
        unique_dirpath = os.path.join(parent_dir, f"{base_name}_{counter}")
        counter += 1
    return unique_dirpath

def generate_unique_filepath(filepath):
    unique_filename = sanitize_filepath(filepath)
    name, ext = os.path.splitext(filepath)
    counter = 1
    while os.path.exists(unique_filename):
        unique_filename = os.path.join(f"{name}_{counter}{ext}")
        counter += 1
    return unique_filename

def write_seg_npz(filepath, full_generation, **kwargs):

    #logger.debug(kwargs)

    if kwargs.get("segment_number", 1) == "base_history":
        filepath = f"{filepath}_initial_prompt.npz"

    if not kwargs.get('dry_run', False) and kwargs.get('always_save_speaker', True):
        filepath = generate_unique_filepath(filepath)
        #np.savez_compressed(filepath, semantic_prompt = full_generation["semantic_prompt"], coarse_prompt = full_generation["coarse_prompt"], fine_prompt = full_generation["fine_prompt"])
        if "semantic_prompt" in full_generation:
            np.savez(filepath, semantic_prompt = full_generation["semantic_prompt"], coarse_prompt = full_generation["coarse_prompt"], fine_prompt = full_generation["fine_prompt"])
        else:
            print("No semantic prompt to save")
        
    

def write_seg_wav(filepath, audio_arr, **kwargs):
    dry_run = kwargs.get('dry_run', False)
    dry_text = '(dry run)' if dry_run else ''
    if dry_run is not True: 
        filepath = generate_unique_filepath(filepath)
        write_audiofile(filepath, audio_arr, **kwargs)





def write_audiofile(output_filepath, audio_arr, **kwargs):
    output_filepath = generate_unique_filepath(output_filepath)

    dry_run =  kwargs.get('dry_run', False)
    dry_text = '(dry run)' if dry_run else ''



    output_format = kwargs.get('output_format', None)

    # print(f"output_format is {output_format}")
    #p rint(f"output_filepath is {output_filepath}")

    if output_format is None: 
        output_format = 'mp3'


    if output_format in ['mp3', 'ogg', 'flac', 'mp4']:
        temp_wav = "{output_filepath}.tmp.wav"
        write_wav(temp_wav, SAMPLE_RATE, audio_arr) if not dry_run else None
        if dry_run is not True:
            audio = AudioSegment.from_wav(temp_wav)
            """
            sample_rate, wav_sample = scipy.io.wavfile.read(temp_wav) 
            audio = AudioSegment(data=wav_sample.tobytes(),
                            sample_width=2,
                            frame_rate=sample_rate, channels=1)
            """
            if output_format == 'mp4':
                audio.export(output_filepath, format="mp4", codec="aac")
            else:
                audio.export(output_filepath, format=output_format)
            os.remove(temp_wav)
    else:
        write_wav(output_filepath, SAMPLE_RATE, audio_arr) if not dry_run else None

    logger.info(f"  .{output_format} saved to {output_filepath} {dry_text}")




def call_with_non_none_params(func, **kwargs):
    non_none_params = {key: value for key, value in kwargs.items() if value is not None}
    return func(**non_none_params)


def generate_audio_barki(
    text: str,
    **kwargs,
):
    """Generate audio array from input text.

    Args:
        text: text to be turned into audio
        history_prompt: history choice for audio cloning
        text_temp: generation temperature (1.0 more diverse, 0.0 more conservative)
        waveform_temp: generation temperature (1.0 more diverse, 0.0 more conservative)
        silent: disable progress bar
        output_full: return full generation to be used as a history prompt


    Returns:
        numpy audio array at sample frequency 24khz
    """
    logger.debug(locals())
    kwargs = load_all_defaults(**kwargs)

    history_prompt = kwargs.get("history_prompt", None)
    text_temp = kwargs.get("text_temp", None)
    waveform_temp = kwargs.get("waveform_temp", None)
    silent = kwargs.get("silent", None)
    output_full = kwargs.get("output_full", None)

    global gradio_try_to_cancel
    global done_cancelling

    seed = kwargs.get("seed",None)
    if seed is not None:
       set_seed(seed)


    ## Semantic Options
    semantic_temp = text_temp
    if kwargs.get("semantic_temp", None):
        semantic_temp = kwargs.get("semantic_temp")

    semantic_seed = kwargs.get("semantic_seed",None)
    if semantic_seed is not None:
       set_seed(semantic_seed)

    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None
    
    confused_travolta_mode = kwargs.get("confused_travolta_mode", False)
    if confused_travolta_mode:
        kwargs["semantic_allow_early_stop"] = False

    semantic_tokens = call_with_non_none_params(
        generate_text_semantic,
        text=text,
        history_prompt=history_prompt,
        temp=semantic_temp,
        top_k=kwargs.get("semantic_top_k", None),
        top_p=kwargs.get("semantic_top_p", None),
        silent=silent,
        min_eos_p = kwargs.get("semantic_min_eos_p", None),
        max_gen_duration_s = kwargs.get("semantic_max_gen_duration_s", None),
        allow_early_stop = kwargs.get("semantic_allow_early_stop", True),
        use_kv_caching=kwargs.get("semantic_use_kv_caching", True),
    )


    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None

    ## Coarse Options
    coarse_temp = waveform_temp
    if kwargs.get("coarse_temp", None):
        coarse_temp = kwargs.get("coarse_temp")

    coarse_seed = kwargs.get("coarse_seed",None)
    if coarse_seed is not None:
       set_seed(coarse_seed)
        
    
    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None
    
    semantic_history_only = kwargs.get("semantic_history_only", False)
    previous_segment_type = kwargs.get("previous_segment_type", '')
    if previous_segment_type == "base_history" and semantic_history_only:
        print(f"previous_segment_type is base_history and semantic_history_only is True. Not forwarding history for for coarse and fine")
        history_prompt = None

    absolute_semantic_history_only = kwargs.get("absolute_semantic_history_only", False)
    if absolute_semantic_history_only:
        print(f"absolute_semantic_history_only is True. Not forwarding history for for coarse and fine")
        history_prompt = None

    absolute_semantic_history_only_every_x = kwargs.get("absolute_semantic_history_only_every_x", None)
    if absolute_semantic_history_only_every_x is not None and absolute_semantic_history_only_every_x > 0:
        segment_number = kwargs.get("segment_number", None)
        if segment_number is not None:
            if segment_number % absolute_semantic_history_only_every_x == 0:
                print(f"segment_number {segment_number} is divisible by {absolute_semantic_history_only_every_x}. Not forwarding history for for coarse and fine")
                history_prompt = None

    coarse_tokens = call_with_non_none_params(
        generate_coarse,
        x_semantic=semantic_tokens,
        history_prompt=history_prompt,
        temp=coarse_temp,
        top_k=kwargs.get("coarse_top_k", None),
        top_p=kwargs.get("coarse_top_p", None),
        silent=silent,
        max_coarse_history=kwargs.get("coarse_max_coarse_history", None),
        sliding_window_len=kwargs.get("coarse_sliding_window_len", None),
        use_kv_caching=kwargs.get("coarse_kv_caching", True),
    )

    fine_temp = kwargs.get("fine_temp", 0.5)

    fine_seed = kwargs.get("fine_seed",None)
    if fine_seed is not None:
       set_seed(fine_seed)

    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None
    fine_tokens = call_with_non_none_params(
        generate_fine,
        x_coarse_gen=coarse_tokens,
        history_prompt=history_prompt,
        temp=fine_temp,
        silent=silent,
    )

    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None
    audio_arr = codec_decode(fine_tokens)
    full_generation = {
        "semantic_prompt": semantic_tokens,
        "coarse_prompt": coarse_tokens,
        "fine_prompt": fine_tokens,
    }

    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None
    
    hoarder_mode = kwargs.get("hoarder_mode", None)
    total_segments = kwargs.get("total_segments", 1)
    if hoarder_mode and (total_segments > 1):
        kwargs["text"] = text
        write_one_segment(audio_arr, full_generation, **kwargs)

    if output_full:
        return full_generation, audio_arr
    
    return audio_arr


def generate_audio_sampling_mods_old(
    text: str,
    **kwargs,
):
    """Generate audio array from input text.

    Args:
        text: text to be turned into audio
        history_prompt: history choice for audio cloning
        text_temp: generation temperature (1.0 more diverse, 0.0 more conservative)
        waveform_temp: generation temperature (1.0 more diverse, 0.0 more conservative)
        silent: disable progress bar
        output_full: return full generation to be used as a history prompt


    Returns:
        numpy audio array at sample frequency 24khz
    """
    logger.debug(locals())
    kwargs = load_all_defaults(**kwargs)

    history_prompt = kwargs.get("history_prompt", None)
    text_temp = kwargs.get("text_temp", None)
    waveform_temp = kwargs.get("waveform_temp", None)
    silent = kwargs.get("silent", None)
    output_full = kwargs.get("output_full", None)

    global gradio_try_to_cancel
    global done_cancelling

    seed = kwargs.get("seed",None)
    if seed is not None:
       set_seed(seed)


    ## Semantic Options
    semantic_temp = text_temp
    if kwargs.get("semantic_temp", None):
        semantic_temp = kwargs.get("semantic_temp")

    semantic_seed = kwargs.get("semantic_seed",None)
    if semantic_seed is not None:
       set_seed(semantic_seed)

    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None
    



    semantic_tokens = call_with_non_none_params(
        generate_text_semantic,
        text=text,
        history_prompt=history_prompt,
        temp=semantic_temp,
        top_k=kwargs.get("semantic_top_k", None),
        top_p=kwargs.get("semantic_top_p", None),
        silent=silent,
        min_eos_p = kwargs.get("semantic_min_eos_p", None),
        max_gen_duration_s = kwargs.get("semantic_max_gen_duration_s", None),
        allow_early_stop = kwargs.get("semantic_allow_early_stop", True),
        use_kv_caching=kwargs.get("semantic_use_kv_caching", True),

        banned_tokens = kwargs.get("semantic_banned_tokens", None),
        absolute_banned_tokens = kwargs.get("semantic_absolute_banned_tokens", None),
        outside_banned_penalty = kwargs.get("semantic_outside_banned_penalty", None),
        target_distribution = kwargs.get("semantic_target_distribution", None),
        target_k_smoothing_factor = kwargs.get("semantic_target_k_smoothing_factor", None),
        target_scaling_factor = kwargs.get("semantic_target_scaling_factor", None),
        history_prompt_distribution = kwargs.get("semantic_history_prompt_distribution", None),
        history_prompt_k_smoothing_factor = kwargs.get("semantic_history_prompt_k_smoothing_factor", None),
        history_prompt_scaling_factor = kwargs.get("semantic_history_prompt_scaling_factor", None),
        history_prompt_average_distribution = kwargs.get("semantic_history_prompt_average_distribution", None),
        history_prompt_average_k_smoothing_factor = kwargs.get("semantic_history_prompt_average_k_smoothing_factor", None),
        history_prompt_average_scaling_factor = kwargs.get("semantic_history_prompt_average_scaling_factor", None),
        target_outside_default_penalty = kwargs.get("semantic_target_outside_default_penalty", None),
        target_outside_outlier_penalty = kwargs.get("semantic_target_outside_outlier_penalty", None),
        history_prompt_unique_voice_penalty = kwargs.get("semantic_history_prompt_unique_voice_penalty", None),
        consider_common_threshold   = kwargs.get("semantic_consider_common_threshold", None),
        history_prompt_unique_voice_threshold = kwargs.get("semantic_history_prompt_unique_voice_threshold", None),
    

    )


    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None

    ## Coarse Options
    coarse_temp = waveform_temp
    if kwargs.get("coarse_temp", None):
        coarse_temp = kwargs.get("coarse_temp")

    coarse_seed = kwargs.get("coarse_seed",None)
    if coarse_seed is not None:
       set_seed(coarse_seed)
        
    
    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None
    
    semantic_history_only = kwargs.get("semantic_history_only", False)
    previous_segment_type = kwargs.get("previous_segment_type", '')
    if previous_segment_type == "base_history" and semantic_history_only is True:
        print(f"previous_segment_type is base_history and semantic_history_only is True. Not forwarding history for for coarse and fine")
        history_prompt = None

    absolute_semantic_history_only = kwargs.get("absolute_semantic_history_only", False)
    if absolute_semantic_history_only:
        print(f"absolute_semantic_history_only is True. Not forwarding history for for coarse and fine")
        history_prompt = None

    absolute_semantic_history_only_every_x = kwargs.get("absolute_semantic_history_only_every_x", None)
    if absolute_semantic_history_only_every_x is not None and absolute_semantic_history_only_every_x > 0:
        segment_number = kwargs.get("segment_number", None)
        if segment_number is not None:
            if segment_number % absolute_semantic_history_only_every_x == 0:
                print(f"segment_number {segment_number} is divisible by {absolute_semantic_history_only_every_x}. Not forwarding history for for coarse and fine")
                history_prompt = None

    coarse_tokens = call_with_non_none_params(
        generate_coarse,
        x_semantic=semantic_tokens,
        history_prompt=history_prompt,
        temp=coarse_temp,
        top_k=kwargs.get("coarse_top_k", None),
        top_p=kwargs.get("coarse_top_p", None),
        silent=silent,
        max_coarse_history=kwargs.get("coarse_max_coarse_history", None),
        sliding_window_len=kwargs.get("coarse_sliding_window_len", None),
        use_kv_caching=kwargs.get("coarse_kv_caching", True),
    )

    fine_temp = kwargs.get("fine_temp", 0.5)

    fine_seed = kwargs.get("fine_seed",None)
    if fine_seed is not None:
       set_seed(fine_seed)

    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None
    fine_tokens = call_with_non_none_params(
        generate_fine,
        x_coarse_gen=coarse_tokens,
        history_prompt=history_prompt,
        temp=fine_temp,
        silent=silent,
    )

    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None
    audio_arr = codec_decode(fine_tokens)
    full_generation = {
        "semantic_prompt": semantic_tokens,
        "coarse_prompt": coarse_tokens,
        "fine_prompt": fine_tokens,
    }

    if gradio_try_to_cancel:
        done_cancelling = True
        return None, None
    
    hoarder_mode = kwargs.get("hoarder_mode", None)

    force_write_segment = kwargs.get("force_write_segment", False)

    total_segments = kwargs.get("total_segments", 1)
    if (hoarder_mode and (total_segments > 1)) or force_write_segment:
        kwargs["text"] = text
        write_one_segment(audio_arr, full_generation, **kwargs)

    if output_full:
        return full_generation, audio_arr
    
    return audio_arr





def generate_audio_long_from_gradio(**kwargs):

       

        full_generation_segments, audio_arr_segments, final_filename_will_be = [],[],None
        
        full_generation_segments, audio_arr_segments, final_filename_will_be = generate_audio_long(**kwargs)

        return full_generation_segments, audio_arr_segments, final_filename_will_be


def generate_audio_long(
    **kwargs,
):

    global gradio_try_to_cancel
    global done_cancelling

    kwargs = load_all_defaults(**kwargs)
    logger.debug(locals())


    history_prompt = None
    history_prompt = kwargs.get("history_prompt", None)
    kwargs["history_prompt"] = None

    silent = kwargs.get("silent", None)
   
    full_generation_segments = []
    audio_arr_segments = []


    
    stable_mode_interval = kwargs.get('stable_mode_interval', None)
    if stable_mode_interval is None:
        stable_mode_interval = 1

    if stable_mode_interval < 0: 
        stable_mode_interval = 0

    stable_mode_interval_counter = None

    if stable_mode_interval >= 2:
        stable_mode_interval_counter = stable_mode_interval

    dry_run = kwargs.get('dry_run', False)

    text_splits_only = kwargs.get('text_splits_only', False)

    if text_splits_only:
        dry_run = True




    # yanked for now,
    extra_confused_travolta_mode = kwargs.get('extra_confused_travolta_mode', None)

    confused_travolta_mode = kwargs.get('confused_travolta_mode', None)

    hoarder_mode = kwargs.get('hoarder_mode', None)

    single_starting_seed = kwargs.get("single_starting_seed",None)
    if single_starting_seed is not None:
        kwargs["seed_return_value"] =set_seed(single_starting_seed)

    # the old way of doing this
    process_text_by_each = kwargs.get("process_text_by_each",None)
    group_text_by_counting = kwargs.get("group_text_by_counting",None)

    if group_text_by_counting is not None and process_text_by_each is not None:
        audio_segments = chunk_up_text_prev(**kwargs)
    else:
        audio_segments = chunk_up_text(**kwargs)

    if text_splits_only:
        print("Nothing was generated, this is just text the splits!")
        return None, None, None

    history_prompt_for_next_segment = None
    base_history = None
    if history_prompt is not None:
        history_prompt_string = history_prompt
        history_prompt = process_history_prompt(history_prompt)
        if history_prompt is not None:
            base_history = np.load(history_prompt)
            base_history = {key: base_history[key] for key in base_history.keys()}
            kwargs['history_prompt_string'] = history_prompt_string
            kwargs["previous_segment_type"] = "base_history"
            history_prompt_for_next_segment = copy.deepcopy(base_history) # just start from a dict for consistency
        else:            
            logger.error(f"Speaker {history_prompt} could not be found, looking in{VALID_HISTORY_PROMPT_DIRS}")

            gradio_try_to_cancel = True
            done_cancelling = True

            return None, None, None

    # way too many files, for hoarder_mode every sample is in own dir
    if hoarder_mode and len(audio_segments) > 1:
        output_dir = kwargs.get('output_dir', "bark_samples")
        output_filename_will_be = determine_output_filename(**kwargs)
        file_name, file_extension = os.path.splitext(output_filename_will_be)
        output_dir_sub = os.path.basename(file_name)
        output_dir = os.path.join(output_dir, output_dir_sub)
        output_dir = generate_unique_dirpath(output_dir)
        kwargs['output_dir'] = output_dir


    if hoarder_mode and kwargs.get("history_prompt_string", False):
        kwargs['segment_number'] = "base_history"
        write_one_segment(audio_arr = None, full_generation = base_history, **kwargs)

    full_generation, audio_arr = (None, None)

    kwargs["output_full"] = True

    # TODO MAKE THIS A PARAM
    # doubled_audio_segments = []
    # doubled_audio_segments = [item for item in audio_segments for _ in range(2)]
    # audio_segments = doubled_audio_segments

    kwargs["total_segments"] = len(audio_segments)


    show_generation_times = kwargs.get("show_generation_times", None)


    all_segments_start_time = time.time()
    


    history_prompt_flipper = False
    for i, segment_text in enumerate(audio_segments):
        estimated_time = estimate_spoken_time(segment_text)
        print(f"segment_text: {segment_text}")

        prompt_text_prefix = kwargs.get("prompt_text_prefix", None)
        if prompt_text_prefix is not None:
            segment_text = f"{prompt_text_prefix} {segment_text}"


        kwargs["text_prompt"] = segment_text
        timeest = f"{estimated_time:.2f}"
        if estimated_time > 14 or estimated_time < 3:
            timeest = f"[bold red]{estimated_time:.2f}[/bold red]"

        current_iteration = str(
            kwargs['current_iteration']) if 'current_iteration' in kwargs else ''
        
        output_iterations = kwargs.get('output_iterations', '')
        iteration_text = ''
        if len(audio_segments) == 1:
            iteration_text = f"{current_iteration} of {output_iterations} iterations"

        segment_number = i + 1
        console.print(f"--Segment {segment_number}/{len(audio_segments)}: est. {timeest}s ({iteration_text})")
        #tqdm.write(f"--Segment {segment_number}/{len(audio_segments)}: est. {timeest}s")  
        #tqdm.set_postfix_str(f"--Segment {segment_number}/{len(audio_segments)}: est. {timeest}s")

        

        if not silent:
            print(f"{segment_text}")
        kwargs['segment_number'] = segment_number

        if dry_run is True:
            full_generation, audio_arr = [], []
        else:




            seperate_prompts = kwargs.get("seperate_prompts", False)
            seperate_prompts_flipper = kwargs.get("seperate_prompts_flipper", False)
            
            if seperate_prompts_flipper is True:
                if seperate_prompts is True:
                    # nice to get actual generation from each speaker 
                    if history_prompt_flipper is True:
                        kwargs['history_prompt'] = None
                        history_prompt_for_next_segment = None
                        history_prompt_flipper = False
                        print(" <History prompt disabled for next segment.>")
                    else:
                        kwargs['history_prompt'] = history_prompt_for_next_segment
                        history_prompt_flipper = True
                else:
                    kwargs['history_prompt'] = history_prompt_for_next_segment
            
            else:
                if seperate_prompts is True:

                        history_prompt_for_next_segment = None
                        print(" <History prompt disabled for next segment.>")
                else:
                    kwargs['history_prompt'] = history_prompt_for_next_segment
          



            if gradio_try_to_cancel:
                done_cancelling = True
                print(" <Cancelled.>")
                return None, None, None
            
            this_segment_start_time = time.time()

            full_generation, audio_arr = generate_audio_barki(text=segment_text, **kwargs)
            
            # if we weren't given a history prompt, save first segment instead



            if gradio_try_to_cancel or full_generation is None or audio_arr is None:
                # Hmn, cancelling and restarting seems to be a bit buggy
                # let's try clearing out stuff
                kwargs = {}
                history_prompt_for_next_segment = None
                base_history = None
                full_generation = None
                done_cancelling = True
                print(" <Cancelled.>")
                return None, None, None      
            

            if show_generation_times:
                this_segment_end_time = time.time()
                elapsed_time = this_segment_end_time - this_segment_start_time


                time_finished = f"Segment Finished at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(this_segment_end_time))}"
                time_taken = f"in {elapsed_time} seconds"
                print(f"  -->{time_finished} {time_taken}")

            # we shouldn't need deepcopy but i'm just throwing darts at the bug
            if base_history is None:
                #print(f"Saving base history for {segment_text}")
                base_history = copy.deepcopy(full_generation)

            logger.debug(f"stable_mode_interval: {stable_mode_interval_counter} of {stable_mode_interval}")



     

            if stable_mode_interval == 0:
                kwargs["previous_segment_type"] = "full_generation"
                history_prompt_for_next_segment = copy.deepcopy(full_generation)
                
                
            elif stable_mode_interval == 1:
                kwargs["previous_segment_type"] = "base_history"
                history_prompt_for_next_segment = copy.deepcopy(base_history)

            elif stable_mode_interval >= 2:
                if stable_mode_interval_counter == 1:
                    # reset to base history
                    stable_mode_interval_counter = stable_mode_interval
                    kwargs["previous_segment_type"] = "base_history"
                    history_prompt_for_next_segment = copy.deepcopy(base_history)
                    logger.info(f"resetting to base history_prompt, again in {stable_mode_interval} chunks")
                else:
                    stable_mode_interval_counter -= 1
                    kwargs["previous_segment_type"] = "full_generation"
                    history_prompt_for_next_segment = copy.deepcopy(full_generation)
            else:
                logger.error(f"stable_mode_interval is {stable_mode_interval} and something has gone wrong.")

                return None, None, None
            


            full_generation_segments.append(full_generation)
            audio_arr_segments.append(audio_arr)

            add_silence_between_segments = kwargs.get("add_silence_between_segments", 0.0)
            if add_silence_between_segments > 0.0:
                # silence = np.zeros(int(add_silence_between_segments * SAMPLE_RATE)) 
                silence =  np.zeros(int(add_silence_between_segments * SAMPLE_RATE), dtype=np.int16 )
                audio_arr_segments.append(silence)

    if show_generation_times:
        all_segments_end_time = time.time()
        elapsed_time = all_segments_end_time - all_segments_start_time


        time_finished = f"All Segments Finished at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(all_segments_end_time))}"
        time_taken = f"in {elapsed_time} seconds"
        print(f"  -->{time_finished} {time_taken}")

    if gradio_try_to_cancel:
        done_cancelling = True
        print("< Cancelled >")
        return None, None, None        

    kwargs['segment_number'] = "final"
    final_filename_will_be = determine_output_filename(**kwargs)
    dry_run = kwargs.get('dry_run', None)
    if not dry_run: 
        write_one_segment(audio_arr = np.concatenate(audio_arr_segments), full_generation = full_generation_segments[0], **kwargs)
    print(f"Saved to {final_filename_will_be}")
    
    return full_generation_segments, audio_arr_segments, final_filename_will_be


def play_superpack_track(superpack_filepath = None, one_random=True):

    try:
        npz_file = np.load(superpack_filepath)

        keys = list(npz_file.keys())
        random_key = random.choice(keys)
        random_prompt = npz_file[random_key].item()
        coarse_tokens = random_prompt["coarse_prompt"]
        fine_tokens = generate_fine(coarse_tokens)
        audio_arr = codec_decode(fine_tokens)

        return audio_arr
    except:
        return None


## TODO can I port the notebook tools somehow?

def doctor_random_speaker_surgery(npz_filepath, gen_minor_variants=5):


    # get directory and filename from npz_filepath
    npz_file_directory, npz_filename = os.path.split(npz_filepath)

    original_history_prompt = np.load(npz_filepath)
    semantic_prompt = original_history_prompt["semantic_prompt"]
    original_semantic_prompt  = copy.deepcopy(semantic_prompt)

    starting_point = 128 
    ending_point = len(original_semantic_prompt) - starting_point

    points = np.linspace(starting_point, ending_point, gen_minor_variants)

    i = 0
    for starting_point in points:
        starting_point = int(starting_point)
        i += 1


        new_semantic_from_beginning = copy.deepcopy(original_semantic_prompt[:starting_point].astype(np.int32))
        new_semantic_from_ending = copy.deepcopy(original_semantic_prompt[starting_point:].astype(np.int32))


        # worse than generating brand new random samples, typically
        for semantic_prompt in [new_semantic_from_beginning, new_semantic_from_ending]:

            #print(f"len(semantic_prompt): {len(semantic_prompt)}")
            #print(f"starting_point: {starting_point}, ending_poinst: {ending_point}") 


            temp_coarse = random.uniform(0.3, 0.90)
            top_k_coarse = None if random.random() < 1/3 else random.randint(25, 400)
            top_p_coarse = None if random.random() < 1/3 else random.uniform(0.90, 0.97)

            max_coarse_history_options = [630, random.randint(500, 630), random.randint(60, 500)]
            max_coarse_history = random.choice(max_coarse_history_options)

            coarse_tokens = generation.generate_coarse(semantic_prompt, temp=temp_coarse, top_k=top_k_coarse, top_p=top_p_coarse, max_coarse_history=max_coarse_history)

            temp_fine = random.uniform(0.3, 0.8)
            fine_tokens = generation.generate_fine(coarse_tokens, temp=temp_fine)

            history_prompt_render_variant = {"semantic_prompt": semantic_prompt, "coarse_prompt": coarse_tokens, "fine_prompt": fine_tokens}

            try:
                audio_arr = generation.codec_decode(fine_tokens)
                base_output_filename = os.path.splitext(npz_filename)[0] + f"_var_{i}.wav"
                output_filepath = os.path.join(npz_file_directory, base_output_filename)
                output_filepath = generate_unique_filepath(output_filepath)
                print(f"output_filepath {output_filepath}")
                print(f"  Rendering minor variant voice audio for {npz_filepath} to {output_filepath}")
                write_seg_wav(output_filepath, audio_arr)

                write_seg_npz(output_filepath, history_prompt_render_variant)
            except:
                # show error
                print(f"  <Error rendering audio for {npz_filepath}>")

def load_npz(filename):
    npz_data = np.load(filename)

    data_dict = {
        "semantic_prompt": npz_data["semantic_prompt"],
        "coarse_prompt": npz_data["coarse_prompt"],
        "fine_prompt": npz_data["fine_prompt"],
    }

    npz_data.close() 

    return data_dict

def render_npz_samples(npz_directory="bark_infinity/assets/prompts/", start_from=None, double_up_history=False, save_npz=False, compression_mode=False, gen_minor_variants=None):
    # Find all the .npz files


    print(f"Rendering samples for speakers in: {npz_directory}")
    npz_files = [f for f in os.listdir(npz_directory) if f.endswith(".npz")]
    

    if start_from is None:
        start_from = "fine_prompt"
    compress_mode_data = []

    for npz_file in npz_files:
        npz_filepath = os.path.join(npz_directory, npz_file)

        history_prompt = load_npz(npz_filepath)

        if not history_prompt_is_valid(history_prompt):
            print(f"Skipping invalid history prompt: {npz_filepath}")
            print(history_prompt_detailed_report(history_prompt))
            continue

        semantic_tokens = history_prompt["semantic_prompt"]
        coarse_tokens = history_prompt["coarse_prompt"]
        fine_tokens = history_prompt["fine_prompt"]

        # print(f"semantic_tokens.shape: {semantic_tokens.shape}")
        # print(f"coarse_tokens.shape: {coarse_tokens.shape}")
        # print(f"fine_tokens.shape: {fine_tokens.shape}")
        

        # this is old and kind of useless, but I'll leave this in UI until I port the better stuff
        if gen_minor_variants is None:
    
            if start_from == "pure_semantic":
                # code removed for now
                semantic_tokens = generate_text_semantic(text=None, history_prompt = history_prompt)
                coarse_tokens = generate_coarse(semantic_tokens)
                fine_tokens = generate_fine(coarse_tokens)

            elif start_from == "semantic_prompt":
                coarse_tokens = generate_coarse(semantic_tokens)
                fine_tokens = generate_fine(coarse_tokens)

            elif start_from == "coarse_prompt":
                fine_tokens = generate_fine(coarse_tokens)
                
            elif start_from == "fine_prompt":
                # just decode existing fine tokens
                pass

            history_prompt_render_variant = {"semantic_prompt": semantic_tokens, "coarse_prompt": coarse_tokens, "fine_prompt": fine_tokens}


        # Not great but it's hooked up to the Gradio UI and does do something guess leave it for now
        elif gen_minor_variants > 0: # gen_minor_variants quick and simple
            print(f"Generating {gen_minor_variants} minor variants for {npz_file}")
            gen_minor_variants = gen_minor_variants or 1
            for i in range(gen_minor_variants):
                temp_coarse = random.uniform(0.3, 0.9)
                top_k_coarse = None if random.random() < 1/3 else random.randint(25, 400)
                top_p_coarse = None if random.random() < 1/3 else random.uniform(0.8, 0.95)

                max_coarse_history_options = [630, random.randint(500, 630), random.randint(60, 500)]
                max_coarse_history = random.choice(max_coarse_history_options)

                coarse_tokens = generate_coarse(semantic_tokens, temp=temp_coarse, top_k=top_k_coarse, top_p=top_p_coarse, max_coarse_history=max_coarse_history)

                temp_fine = random.uniform(0.3, 0.7)
                fine_tokens = generate_fine(coarse_tokens, temp=temp_fine)

                history_prompt_render_variant = {"semantic_prompt": semantic_tokens, "coarse_prompt": coarse_tokens, "fine_prompt": fine_tokens}

                try:
                    audio_arr = codec_decode(fine_tokens)
                    base_output_filename = os.path.splitext(npz_file)[0] + f"_var_{i}.wav"
                    output_filepath = os.path.join(npz_directory, base_output_filename)
                    output_filepath = generate_unique_filepath(output_filepath)
                    print(f"  Rendering minor variant voice audio for {npz_filepath} to {output_filepath}")
                    write_seg_wav(output_filepath, audio_arr)

                    write_seg_npz(output_filepath, history_prompt_render_variant)
                except:
                    print(f"  <Error rendering audio for {npz_filepath}>")

        
        if not compression_mode:
            start_from_txt = ''

            if start_from == "semantic_prompt":
                start_from_txt = '_W'
            elif start_from == "coarse_prompt":
                start_from_txt = '_S'
            try:
                #print(f"fine_tokens.shape final: {fine_tokens.shape}")
                audio_arr = codec_decode(fine_tokens)
                base_output_filename = os.path.splitext(npz_file)[0] + f"_{start_from_txt}_.wav"
                output_filepath = os.path.join(npz_directory, base_output_filename)
                output_filepath = generate_unique_filepath(output_filepath)
                print(f"  Rendering audio for {npz_filepath} to {output_filepath}")
                write_seg_wav(output_filepath, audio_arr)
                if save_npz and start_from != "fine_prompt":
                    write_seg_npz(output_filepath, history_prompt_render_variant)
            except Exception as e:
                print(f"  <Error rendering audio for {npz_filepath}>")
                print(f"  Error details: {e}")
        elif compression_mode:
            just_record_it = {"semantic_prompt": None, "coarse_prompt": coarse_tokens, "fine_prompt": None}
            compress_mode_data.append(just_record_it)
            #compress_mode_data.append(history_prompt_render_variant)

    # defunct
    if compression_mode:
        print(f"have {len(compress_mode_data)} samples")
        output_filepath = os.path.join(npz_directory, "superpack.npz")
        output_filepath = generate_unique_filepath(output_filepath)
        with open(f"{output_filepath}", 'wb') as f:
            np.savez_compressed(f, **{f"dict_{i}": np.array([d]) for i, d in enumerate(compress_mode_data)})

        



def resize_semantic_history(semantic_history, weight, max_len=256):

    new_len = int(max_len * weight)

    semantic_history = semantic_history.astype(np.int64)
    # Trim 
    if len(semantic_history) > new_len:
        semantic_history = semantic_history[-new_len:]
    # Pad 
    else:
        semantic_history = np.pad(
            semantic_history,
            (0, new_len - len(semantic_history)),
            constant_values=SEMANTIC_PAD_TOKEN,
            mode="constant",
        )

    return semantic_history



def estimate_spoken_time(text, wpm=150, threshold=15):
    text_without_brackets = re.sub(r'\[.*?\]', '', text)

    words = text_without_brackets.split()
    word_count = len(words)
    time_in_seconds = (word_count / wpm) * 60
    return time_in_seconds



def chunk_up_text(**kwargs):

    text_prompt = kwargs['text_prompt']
    split_character_goal_length = kwargs['split_character_goal_length']
    split_character_max_length = kwargs['split_character_max_length']
    silent = kwargs.get('silent')


    split_character_jitter = kwargs.get('split_character_jitter') or 0

    if split_character_jitter > 0:
        split_character_goal_length = random.randint(split_character_goal_length - split_character_jitter, split_character_goal_length + split_character_jitter)
        split_character_max_length = random.randint(split_character_max_length - split_character_jitter, split_character_max_length + split_character_jitter)




    audio_segments = text_processing.split_general_purpose(text_prompt, split_character_goal_length=split_character_goal_length, split_character_max_length=split_character_max_length)


    split_desc = f"Splitting long text aiming for {split_character_goal_length} chars max {split_character_max_length}"

    if (len(audio_segments) > 0):
        print_chunks_table(audio_segments, left_column_header="Words",
                           right_column_header=split_desc, **kwargs) if not silent else None
    return audio_segments



def chunk_up_text_prev(**kwargs):

    text_prompt = kwargs['text_prompt']
    process_text_by_each = kwargs['process_text_by_each']
    in_groups_of_size = kwargs['in_groups_of_size']
    group_text_by_counting = kwargs.get('group_text_by_counting',None)
    split_type_string = kwargs.get('split_type_string','')
    
    silent = kwargs.get('silent')

    audio_segments = text_processing.split_text(text_prompt, split_type = process_text_by_each, split_type_quantity = in_groups_of_size, split_type_string = split_type_string, split_type_value_type = group_text_by_counting)

    split_desc = f"Processing text by {process_text_by_each} grouping by {group_text_by_counting} in {in_groups_of_size}, str: {split_type_string} "
 
    if (len(audio_segments) > 0):
        print_chunks_table(audio_segments, left_column_header="Words",
                           right_column_header=split_desc, **kwargs) if not silent else None
    return audio_segments



def print_chunks_table(chunks: list, left_column_header: str = "Words", right_column_header: str = "Segment Text", **kwargs):

    output_iterations = kwargs.get('output_iterations', '')

    current_iteration = str(
        kwargs['current_iteration']) if 'current_iteration' in kwargs else ''
    
    iteration_text = ''
    if output_iterations and current_iteration:
        
        iteration_text = f"{current_iteration} of {output_iterations} iterations"
    
    table = Table(
        title=f"    ({iteration_text}) Segment Breakdown", show_lines=True, title_justify = "left")
    table.add_column('#', justify="right", style="magenta", no_wrap=True)
    table.add_column(left_column_header, style="green")
    table.add_column("Time Est", style="green")
    table.add_column(right_column_header)
    i = 1


    for chunk in chunks:
        timeest = f"{estimate_spoken_time(chunk):.2f} s"
        if estimate_spoken_time(chunk) > 14:
            timeest = f"!{timeest}!"
        wordcount = f"{str(len(chunk.split()))}"
        charcount = f"{str(len(chunk))}"
        table.add_row(str(i), f"{str(len(chunk.split()))}", f"{timeest}\n{charcount} chars", chunk)
        i += 1
    console.print(table)




LANG_CODE_DICT = {code: lang for lang, code in generation.SUPPORTED_LANGS}


def gather_speakers(directory):
    speakers = defaultdict(list)
    unsupported_files = []

    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith('.npz'):
                match = re.match(r"^([a-z]{2})_.*", filename)
                if match and match.group(1) in LANG_CODE_DICT:
                    speakers[match.group(1)].append(os.path.join(root, filename))
                else:
                    unsupported_files.append(os.path.join(root, filename))

    return speakers, unsupported_files

def list_speakers():
    all_speakers = defaultdict(list)
    all_unsupported_files = []

    for directory in VALID_HISTORY_PROMPT_DIRS:
        speakers, unsupported_files = gather_speakers(directory)
        all_speakers.update(speakers)
        all_unsupported_files.extend(unsupported_files)

    print_speakers(all_speakers, all_unsupported_files)

    return all_speakers, all_unsupported_files


def print_speakers(speakers, unsupported_files):
    # Print speakers grouped by language code
    for lang_code, files in speakers.items():
        print(LANG_CODE_DICT[lang_code] + ":")
        for file in files:
            print("  " + file)

    # Print unsupported files
    print("Other:")
    for file in unsupported_files:
        print("  " + file)







from collections import Counter

CONTEXT_WINDOW_SIZE = 1024

SEMANTIC_RATE_HZ = 49.9
SEMANTIC_VOCAB_SIZE = 10_000

CODEBOOK_SIZE = 1024
N_COARSE_CODEBOOKS = 2
N_FINE_CODEBOOKS = 8
COARSE_RATE_HZ = 75

SAMPLE_RATE = 24_000

TEXT_ENCODING_OFFSET = 10_048
SEMANTIC_PAD_TOKEN = 10_000
TEXT_PAD_TOKEN = 129_595
SEMANTIC_INFER_TOKEN = 129_599

def generate_text_semantic_report(history_prompt, token_samples=3):

    semantic_history = history_prompt["semantic_prompt"]

    report = {"valid": True, "messages": []}
    
    if not isinstance(semantic_history, np.ndarray) and not isinstance(semantic_history, torch.Tensor): 
        report["valid"] = False
        report["messages"].append(f"should be a numpy array but was {type(semantic_history)}.")
    
    elif len(semantic_history.shape) != 1:
        report["valid"] = False
        report["messages"].append(f"should be a 1d numpy array but shape was {semantic_history.shape}.")
    
    elif len(semantic_history) == 0:
        report["valid"] = False
        report["messages"].append("should not be empty.")
    
    else:
        if semantic_history.min() < 0:
            report["valid"] = False
            report["messages"].append(f"minimum value of 0, but it was {semantic_history.min()}.")
            index = np.argmin(semantic_history)
            surrounding = semantic_history[max(0, index - token_samples) : min(len(semantic_history), index + token_samples)]
            report["messages"].append(f"Surrounding tokens: {surrounding}")
            
        elif semantic_history.max() >= SEMANTIC_VOCAB_SIZE:
            report["valid"] = False
            report["messages"].append(f"should have a maximum value less than {SEMANTIC_VOCAB_SIZE}, but it was {semantic_history.max()}.")
            index = np.argmax(semantic_history)
            surrounding = semantic_history[max(0, index - token_samples) : min(len(semantic_history), index + token_samples)]
            report["messages"].append(f"Surrounding tokens: {surrounding}")
            
    return report


def generate_coarse_report(history_prompt, token_samples=3):

    semantic_to_coarse_ratio = COARSE_RATE_HZ / SEMANTIC_RATE_HZ * N_COARSE_CODEBOOKS

    semantic_history = history_prompt["semantic_prompt"]
    coarse_history = history_prompt["coarse_prompt"]

    report = {"valid": True, "messages": []}
    
    if not isinstance(semantic_history, np.ndarray) and not isinstance(semantic_history, torch.Tensor):
        report["valid"] = False
        report["messages"].append(f"should be a numpy array but it's a {type(semantic_history)}.")
    
    elif len(semantic_history.shape) != 1:
        report["valid"] = False
        report["messages"].append("should be a 1d numpy array but shape is {semantic_history.shape}.")
    
    elif len(semantic_history) == 0:
        report["valid"] = False
        report["messages"].append("should not be empty.")
    else:
        
        if semantic_history.min() < 0:
            report["valid"] = False
            report["messages"].append(f"should have a minimum value of 0, but it was {semantic_history.min()}.")
            index = np.argmin(semantic_history)
            surrounding = semantic_history[max(0, index - token_samples) : min(len(semantic_history), index + token_samples)]
            report["messages"].append(f"Surrounding tokens: {surrounding}")
            
        elif semantic_history.max() >= SEMANTIC_VOCAB_SIZE:
            report["valid"] = False
            report["messages"].append(f"should have a maximum value less than {SEMANTIC_VOCAB_SIZE}, but it was {semantic_history.max()}.")
            index = np.argmax(semantic_history)
            surrounding = semantic_history[max(0, index - token_samples) : min(len(semantic_history), index + token_samples)]
            report["messages"].append(f"Surrounding tokens: {surrounding}")
        
    if not isinstance(coarse_history, np.ndarray):
        report["valid"] = False
        report["messages"].append(f"should be a numpy array but it's a {type(coarse_history)}.")
        
    elif len(coarse_history.shape) != 2:
        report["valid"] = False
        report["messages"].append(f"should be a 2-dimensional numpy array but shape is {coarse_history.shape}.")
        
    elif coarse_history.shape[0] != N_COARSE_CODEBOOKS:
        report["valid"] = False
        report["messages"].append(f"should have {N_COARSE_CODEBOOKS} rows, but it has {coarse_history.shape[0]}.")

    elif coarse_history.size == 0:
        report["valid"] = False
        report["messages"].append("The coarse history should not be empty.")
    
    else:    
        if coarse_history.min() < 0:
            report["valid"] = False
            report["messages"].append(f"should have a minimum value of 0, but it was {coarse_history.min()}.")
            indices = np.unravel_index(coarse_history.argmin(), coarse_history.shape)
            surrounding = coarse_history[max(0, indices[1] - token_samples) : min(coarse_history.shape[1], indices[1] + token_samples)]
            report["messages"].append(f"Surrounding tokens in row {indices[0]}: {surrounding}")
            
        elif coarse_history.max() >= CODEBOOK_SIZE:
            report["valid"] = False
            report["messages"].append(f"should have a maximum value less than {CODEBOOK_SIZE}, but it was {coarse_history.max()}.")
            indices = np.unravel_index(coarse_history.argmax(), coarse_history.shape)
            surrounding = coarse_history[max(0, indices[1] - token_samples) : min(coarse_history.shape[1], indices[1] + token_samples)]
            report["messages"].append(f"Surrounding tokens in row {indices[0]}: {surrounding}")
        
        ratio = round(coarse_history.shape[1] / len(semantic_history), 1)
        if ratio != round(semantic_to_coarse_ratio / N_COARSE_CODEBOOKS, 1):
            report["valid"] = False
            report["messages"].append(f"ratio should be {round(semantic_to_coarse_ratio / N_COARSE_CODEBOOKS, 1)}, but it was {ratio}.")

    return report


def generate_fine_report(history_prompt, token_samples=3):

    fine_history = history_prompt["fine_prompt"]

    report = {"valid": True, "messages": []}
    
    if not isinstance(fine_history, np.ndarray):
        report["valid"] = False
        report["messages"].append("fine_prompt should be a numpy array but it's a {type(fine_history)}.")
    
    elif len(fine_history.shape) != 2:
        report["valid"] = False
        report["messages"].append("fine_prompt should be a 2-dimensional numpy array but shape is {fine_history.shape}.")

    elif fine_history.size == 0:
        report["valid"] = False
        report["messages"].append("fine_prompt should not be empty.")
    
    else:
    
        if fine_history.shape[0] != N_FINE_CODEBOOKS:
            report["valid"] = False
            report["messages"].append(f"fine_prompt should have {N_FINE_CODEBOOKS} rows, but it has {fine_history.shape[0]}.")
            
        elif fine_history.min() < 0:
            report["valid"] = False
            report["messages"].append(f"fine_prompt should have a minimum value of 0, but it was {fine_history.min()}.")
            indices = np.unravel_index(fine_history.argmin(), fine_history.shape)
            surrounding = fine_history[max(0, indices[1] - token_samples) : min(fine_history.shape[1], indices[1] + token_samples)]
            report["messages"].append(f"Surrounding tokens in row {indices[0]}: {surrounding}")
            
        elif fine_history.max() >= CODEBOOK_SIZE:
            report["valid"] = False
            report["messages"].append(f"fine_prompt should have a maximum value less than {CODEBOOK_SIZE}, but it was {fine_history.max()}.")
            indices = np.unravel_index(fine_history.argmax(), fine_history.shape)
            surrounding = fine_history[max(0, indices[1] - token_samples) : min(fine_history.shape[1], indices[1] + token_samples)]
            report["messages"].append(f"Surrounding tokens in row {indices[0]}: {surrounding}")
        
    return report


def display_history_prompt_report(report):
    if report["valid"]:
        print("valid")
    else:
        print("history_prompt failed the following checks:")
        for i, message in enumerate(report["messages"], start=1):
            print(f"  Error {i}: {message}")

def history_prompt_is_valid(history_prompt):

    try:
        history_prompt = generation._load_history_prompt(history_prompt)
    except Exception as e:
        print(f"Error: {str(e)}")
        return
    

    semantic_report = generate_text_semantic_report(history_prompt)
    coarse_report = generate_coarse_report(history_prompt)
    fine_report = generate_fine_report(history_prompt)
    return semantic_report["valid"] and coarse_report["valid"] and fine_report["valid"]


def history_prompt_detailed_report(history_prompt, token_samples=3):
    try:
        history_prompt = generation._load_history_prompt(history_prompt)
    except Exception as e:
        print(f"Error: {str(e)}")
        return
    
    file_name = None
    if isinstance(history_prompt, str):
        file_name = history_prompt

    if file_name:
        print(f"\n>>{file_name}")

    try:
        text_semantic_report = generate_text_semantic_report(history_prompt, token_samples)
        print("\n  Semantic:")
        display_history_prompt_report(text_semantic_report)
    except Exception as e:
        print(f"Error generating Text Semantic Report: {str(e)}")

    try:
        coarse_report = generate_coarse_report(history_prompt, token_samples)
        print("\n  Coarse:")
        display_history_prompt_report(coarse_report)
    except Exception as e:
        print(f"Error generating Coarse Report: {str(e)}")

    try:
        fine_report = generate_fine_report(history_prompt, token_samples)
        print("\n  Fine:")
        display_history_prompt_report(fine_report)
    except Exception as e:
        print(f"Error generating Fine Report: {str(e)}")



from codex_autorunner.voice.providers.openai_whisper import _pick_upload_content_type


def test_pick_upload_content_type_strips_codec_parameters():
    assert (
        _pick_upload_content_type("voice.webm", "audio/webm;codecs=opus")
        == "audio/webm"
    )


def test_pick_upload_content_type_maps_video_webm_to_audio_webm():
    assert _pick_upload_content_type("voice.webm", "video/webm") == "audio/webm"


def test_pick_upload_content_type_maps_m4a_extension_to_audio_mp4():
    assert _pick_upload_content_type("voice.m4a", None) == "audio/mp4"


def test_pick_upload_content_type_maps_weird_mp4a_latm_to_audio_mp4():
    assert _pick_upload_content_type("voice.m4a", "audio/mp4a-latm") == "audio/mp4"

from app.services.separator import DEFAULT_MODEL, PythonAudioSeparatorEngine


def test_default_model_is_deployment_configurable(tmp_path):
    engine = PythonAudioSeparatorEngine(tmp_path, default_model="cpu-model.onnx")

    assert engine.resolve_model("default") == "cpu-model.onnx"
    assert engine.resolve_model("roformer") == DEFAULT_MODEL
    assert engine.resolve_model("mdx") == "UVR-MDX-NET-Inst_HQ_3.onnx"
    assert engine.resolve_model("clean_vocals") == "Kim_Vocal_1.onnx"

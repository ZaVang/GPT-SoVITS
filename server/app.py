import os
import shutil
import sys
from typing import Any, Optional
import zipfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import chardet
from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, APIRouter
from contextlib import asynccontextmanager
from pydantic import BaseModel, model_validator
import json
import tempfile
import soundfile as sf
import logging
from logging.handlers import RotatingFileHandler
from starlette.responses import FileResponse
import gradio as gr
import uvicorn
import pandas as pd

from server.asr import audio_line_check
from server.webui import webui, LANG_DICT
from server.modelhandler import ModelHandler
from src.inference import TTSInference

root_logger = logging.getLogger()

gpt_model_handler = ModelHandler('pretrained_models/gpt_weights/')
sovits_model_handler = ModelHandler('pretrained_models/sovits_weights/')
tts_inference = TTSInference(is_half=False)
with open('server/example.json', 'r') as f:
    example_json = json.load(f)

# 模型请求参数数据模型
class TTSModelRequest(BaseModel):
    character_name: Optional[str] = None
    ref_audio_path: Optional[str] = None
    sovits_weights: Optional[str] = None
    gpt_weights: Optional[str] = None
    prompt_text: Optional[str] = None
    prompt_language: Optional[str] = None
    text: str
    text_language: str
    how_to_cut: Optional[str] = "不切"
    top_k: Optional[int] = 5
    top_p: Optional[float] = 0.7
    temperature: Optional[float] = 0.7
    ref_free: Optional[bool] = False
    zip_filename: Optional[str] = None
    
    @model_validator(mode='before')
    @classmethod
    def validate_to_json(cls, value: Any) -> Any:
        print(value)
        if isinstance(value, str):
            return cls(**json.loads(value))
        return value
    
    
async def remove_temp_file(path: str):
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)

def setup_logging():
    #设置根日志记录器
    root_logger.setLevel(logging.INFO)

    rotating_handler = RotatingFileHandler('app.log')
    rotating_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    root_logger.addHandler(rotating_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    root_logger.addHandler(stream_handler)
        
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 设置日志
    setup_logging()
    yield
    
app = FastAPI(lifespan=lifespan)
router = APIRouter()


@app.exception_handler(Exception)
async def exception_handler(request, exc):
    return {"error": str(exc)}, 500

# @router.post("/api/tts/upload-audio")
# async def upload_audio(audio_file: UploadFile):
#     return {"filename": audio_file.filename}


@router.post("/api/tts/inference")
async def predict(
    #   audio_file: UploadFile, 
    data: TTSModelRequest, 
    background_tasks: BackgroundTasks
):
    try:
        if data.character_name is not None:
            data.ref_audio_path = example_json[data.character_name]['audio_path']
            data.prompt_text = example_json[data.character_name]['ref_text']
            data.prompt_language = example_json[data.character_name]['ref_language']
            data.gpt_weights = example_json[data.character_name]['gpt_weights']
            data.sovits_weights = example_json[data.character_name]['sovits_weights']
        if tts_inference.sovits_model != data.sovits_weights:
            tts_inference.change_sovits_weights(data.sovits_weights) # type: ignore
        if tts_inference.gpt_model!= data.gpt_weights:
            tts_inference.change_gpt_weights(data.gpt_weights) # type: ignore
        # 进行预测
        try:
            print('generating......', flush=True)
            audio_generator = tts_inference.infer(
                data.ref_audio_path, # type: ignore
                data.prompt_text,
                LANG_DICT[data.prompt_language], # type: ignore
                data.text,
                LANG_DICT[data.text_language], # type: ignore
                how_to_cut=data.how_to_cut, # type: ignore
                top_k=data.top_k, # type: ignore
                top_p=data.top_p, # type: ignore
                temperature=data.temperature, # type: ignore
                ref_free=data.ref_free) # type: ignore
            
            sr, audio = next(audio_generator)
            print('generation finished!', flush=True)

        except Exception as e:
            root_logger.error(f"Error during inference: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Error during inference")
        
        # 创建一个临时文件来保存音频数据
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        sf.write(temp_file.name, audio, sr, 'PCM_24')
        
        # 在后台删除临时文件
        background_tasks.add_task(remove_temp_file, temp_file.name)

        return FileResponse(temp_file.name, media_type='audio/wav')  # 发送文件

    except KeyError as e:
        return {"error": f"Missing necessary parameter: {e.args[0]}"}, 400


@router.post("/api/tts/batch_inference")
async def batch_predict(
    data: TTSModelRequest,
    excel_file: UploadFile, 
    background_tasks: BackgroundTasks
):
    try:
        # 读取上传的Excel文件
        contents = await excel_file.read()
        df = pd.read_excel(contents, header=0)  # 假设第一行是表头
        texts = df.iloc[:, 0].tolist()  # 跳过表头
        filenames = df.iloc[:, 1].tolist()  # 跳过表头
        
        if data.character_name is not None:
            data.ref_audio_path = example_json[data.character_name]['audio_path']
            data.prompt_text = example_json[data.character_name]['ref_text']
            data.prompt_language = example_json[data.character_name]['ref_language']
            data.gpt_weights = example_json[data.character_name]['gpt_weights']
            data.sovits_weights = example_json[data.character_name]['sovits_weights']
        if tts_inference.sovits_model != data.sovits_weights:
            tts_inference.change_sovits_weights(data.sovits_weights) # type: ignore
        if tts_inference.gpt_model!= data.gpt_weights:
            tts_inference.change_gpt_weights(data.gpt_weights) # type: ignore
        audio_files = []
        for text, filename in zip(texts, filenames):
            # 进行预测
            try:
                print('generating......', flush=True)
                audio_generator = tts_inference.infer(
                    data.ref_audio_path, # type: ignore
                    data.prompt_text,
                    LANG_DICT[data.prompt_language], # type: ignore
                    text,
                    LANG_DICT[data.text_language], # type: ignore
                    how_to_cut=data.how_to_cut, # type: ignore
                    top_k=data.top_k, # type: ignore
                    top_p=data.top_p, # type: ignore
                    temperature=data.temperature, # type: ignore
                    ref_free=data.ref_free) # type: ignore
                
                sr, audio = next(audio_generator)
                print('generation finished!', flush=True)

            except Exception as e:
                root_logger.error(f"Error during inference: {str(e)}", exc_info=True)
                raise HTTPException(status_code=500, detail="Error during inference")
            
            # 创建一个临时文件来保存音频数据
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
            sf.write(temp_file.name, audio, sr, 'PCM_24')
            
            # 重命名临时文件为指定的文件名
            final_path = os.path.join(tempfile.gettempdir(), filename)
            os.rename(temp_file.name, final_path)
            
            audio_files.append(final_path)

        # 将所有音频文件打包成一个zip文件
        zip_file = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        with zipfile.ZipFile(zip_file.name, 'w') as zipf:
            for audio_file in audio_files:
                zipf.write(audio_file, os.path.basename(audio_file))
        if data.zip_filename is not None:
            final_zip_path = os.path.join(tempfile.gettempdir(), data.zip_filename)
            os.rename(zip_file.name, final_zip_path)
        
        # 在后台删除音频文件和zip文件
        for audio_file in audio_files:
            background_tasks.add_task(remove_temp_file, audio_file)
        background_tasks.add_task(remove_temp_file, final_zip_path)

        return FileResponse(final_zip_path, media_type='application/zip')  # 发送zip文件

    except KeyError as e:
        return {"error": f"Missing necessary parameter: {e.args[0]}"}, 400


# @router.post("/api/tts/check_lines")
# async def check_lines(file: UploadFile, background_tasks: BackgroundTasks):
#     # 创建临时目录
#     temp_dir = "temp"
#     if not os.path.exists(temp_dir):
#         os.makedirs(temp_dir)
    
#     # 保存上传的zip文件
#     zip_path = os.path.join(temp_dir, file.filename) # type: ignore
#     with open(zip_path, "wb") as buffer:
#         shutil.copyfileobj(file.file, buffer)
    
#     # 解压zip文件
#     with zipfile.ZipFile(zip_path, 'r') as zip_ref:
#         for member in zip_ref.infolist():
#             encoding = chardet.detect(member.filename.encode('cp437'))['encoding']
#             if encoding is None:
#                 encoding = 'utf-8'  # 默认使用utf-8
#             member.filename = member.filename.encode('cp437').decode(encoding)
#             zip_ref.extract(member, temp_dir)
    
#     # 假设解压后的文件夹结构是固定的
#     base_name = os.path.splitext(os.path.basename(zip_path))[0]
#     base_folder = os.path.join(temp_dir, base_name)
    
#     # 直接构建路径
#     excel_path = os.path.join(base_folder, "需求表.xlsx")
#     audio_folder = os.path.join(base_folder, "配音文件")
    
#     if not excel_path or not audio_folder:
#         return {"error": "需求表.xlsx或配音文件夹未找到"}
    
#     # 执行audio_line_check函数
#     await audio_line_check(excel_path, audio_folder)
    
#     # 获取审查结果文件路径
#     result_excel_path = excel_path.replace("需求表.xlsx", "审查结果.xlsx")
    
#     # 在后台删除临时目录
#     background_tasks.add_task(remove_temp_file, temp_dir)

#     # 返回审查结果文件
#     return FileResponse(result_excel_path, filename="审查结果.xlsx")

# 挂载 Gradio 接口到 FastAPI 应用
ui = webui()
app = gr.mount_gradio_app(app, ui, path="/ai-speech/api/gradio")
app.include_router(router, prefix="/ai-speech")


if __name__ == '__main__':
    # 运行Uvicorn服务器
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="info")
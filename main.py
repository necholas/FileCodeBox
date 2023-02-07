import asyncio
import datetime
import uuid
from pathlib import Path
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import HTMLResponse, FileResponse
from starlette.staticfiles import StaticFiles
from core.utils import error_ip_limit, upload_ip_limit, get_code, storage, delete_expire_files
from core.depends import admin_required
from fastapi import FastAPI, Depends, UploadFile, Form, File, HTTPException, BackgroundTasks
from core.database import init_models, Options, Codes, get_session
from settings import settings

# 实例化FastAPI
app = FastAPI(debug=settings.DEBUG, redoc_url=None, docs_url=None, openapi_url=None)


# 触发事件
@app.on_event('startup')
async def startup(s: AsyncSession = Depends(get_session)):
    # 初始化数据库
    await init_models(s)
    # 启动后台任务，不定时删除过期文件
    asyncio.create_task(delete_expire_files())


# 数据存储文件夹
DATA_ROOT = Path(settings.DATA_ROOT)
if not DATA_ROOT.exists():
    DATA_ROOT.mkdir(parents=True)

# 静态文件夹，这个固定就行了，静态资源都放在这里
app.mount('/static', StaticFiles(directory='./static'), name="static")

# 首页页面
index_html = open('templates/index.html', 'r', encoding='utf-8').read()
# 管理页面
admin_html = open('templates/admin.html', 'r', encoding='utf-8').read()


@app.get('/')
async def index():
    return HTMLResponse(
        index_html.replace('{{title}}', settings.TITLE).replace('{{description}}', settings.DESCRIPTION).replace(
            '{{keywords}}', settings.KEYWORDS).replace("'{{fileSizeLimit}}'", str(settings.FILE_SIZE_LIMIT))
    )


@app.get(f'/{settings.ADMIN_ADDRESS}', description='管理页面')
async def admin():
    return HTMLResponse(
        admin_html.replace('{{title}}', settings.TITLE).replace('{{description}}', settings.DESCRIPTION).replace(
            '{{admin_address}}', settings.ADMIN_ADDRESS).replace('{{keywords}}', settings.KEYWORDS)
    )


@app.post(f'/{settings.ADMIN_ADDRESS}', dependencies=[Depends(admin_required)], description='查询数据库列表')
async def admin_post(page: int = Form(default=1), size: int = Form(default=10), s: AsyncSession = Depends(get_session)):
    infos = (await s.execute(select(Codes).offset((page - 1) * size).limit(size))).scalars().all()
    data = [{
        'id': info.id,
        'code': info.code,
        'name': info.name,
        'exp_time': info.exp_time,
        'count': info.count,
        'text': info.text if info.type == 'text' else await storage.get_url(info),
    } for info in infos]
    return {
        'detail': '查询成功',
        'data': data,
        'paginate': {
            'page': page,
            'size': size,
            'total': (await s.execute(select(func.count(Codes.id)))).scalar()
        }}


@app.delete(f'/{settings.ADMIN_ADDRESS}', dependencies=[Depends(admin_required)], description='删除数据库记录')
async def admin_delete(code: str, s: AsyncSession = Depends(get_session)):
    # 找到相应记录
    query = select(Codes).where(Codes.code == code)
    # 找到第一条记录
    file = (await s.execute(query)).scalars().first()
    # 如果记录存在，并且不是文本
    if file and file.type != 'text':
        # 删除文件
        await storage.delete_file(file.text)
    # 删除数据库记录
    await s.delete(file)
    await s.commit()
    return {'detail': '删除成功'}


@app.get(f'/{settings.ADMIN_ADDRESS}/config', description='获取系统配置', dependencies=[Depends(admin_required)])
async def config(s: AsyncSession = Depends(get_session)):
    # 查询数据库
    data = {}
    for i in (await s.execute(select(Options))).scalars().all():
        data[i.key] = i.value
    return {'detail': '获取成功', 'data': data, 'menus': [
        {'key': 'INSTALL', 'name': '版本信息'},
        {'key': 'WEBSITE', 'name': '网站设置'},
        {'key': 'SHARE', 'name': '分享设置'},
        {'key': 'BANNERS', 'name': 'Banner'},
    ]}


@app.patch(f'/{settings.ADMIN_ADDRESS}', dependencies=[Depends(admin_required)], description='修改数据库数据')
async def admin_patch(request: Request, s: AsyncSession = Depends(get_session)):
    data = await request.json()
    data.pop('INSTALL')
    for key, value in data.items():
        await s.execute(update(Options).where(Options.key == key).values(value=value))
        await settings.update(key, value)
    await s.commit()
    await settings.updates([[i.id, i.key, i.value] for i in (await s.execute(select(Options))).scalars().all()])
    return {'detail': '修改成功'}


@app.post('/')
async def index(code: str, ip: str = Depends(error_ip_limit), s: AsyncSession = Depends(get_session)):
    """
    上传功能首页
    :param code:
    :param ip:
    :param s:
    :return:
    """
    query = select(Codes).where(Codes.code == code)
    info = (await s.execute(query)).scalars().first()
    if not info:
        error_count = settings.ERROR_COUNT - error_ip_limit.add_ip(ip)
        raise HTTPException(status_code=404, detail=f"取件码错误，{error_count}次后将被禁止{settings.ERROR_MINUTE}分钟")
    if info.exp_time < datetime.datetime.now() or info.count == 0:
        raise HTTPException(status_code=404, detail="取件码已失效，请联系寄件人")
    await s.execute(update(Codes).where(Codes.id == info.id).values(count=info.count - 1))
    await s.commit()
    if info.type != 'text':
        info.text = await storage.get_url(info)
    return {
        'detail': f'取件成功，请立即下载，避免失效！',
        'data': {'type': info.type, 'text': info.text, 'name': info.name, 'code': info.code}
    }


@app.get('/banner')
async def banner(request: Request):
    # 数据库查询config
    return {
        'detail': '查询成功',
        'data': settings.BANNERS,
        'enable': request.headers.get('pwd', '') == settings.ADMIN_PASSWORD or settings.ENABLE_UPLOAD,
    }


@app.get('/select')
async def get_file(code: str, ip: str = Depends(error_ip_limit), s: AsyncSession = Depends(get_session)):
    # 查出数据库记录
    query = select(Codes).where(Codes.code == code)
    info = (await s.execute(query)).scalars().first()
    # 如果记录不存在，IP错误次数+1
    if not info:
        error_ip_limit.add_ip(ip)
        raise HTTPException(status_code=404, detail="口令不存在，次数过多将被禁止访问")
    # 如果是文本，直接返回
    if info.type == 'text':
        return {'detail': '查询成功', 'data': info.text}
    # 如果是文件，返回文件
    else:
        filepath = await storage.get_filepath(info.text)
        return FileResponse(filepath, filename=info.name)


@app.post('/share', dependencies=[Depends(admin_required)], description='分享文件')
async def share(background_tasks: BackgroundTasks, text: str = Form(default=None),
                style: str = Form(default='2'), value: int = Form(default=1), file: UploadFile = File(default=None),
                ip: str = Depends(upload_ip_limit), s: AsyncSession = Depends(get_session)):
    code = await get_code(s)
    if style == '2':
        if value > settings.MAX_DAYS:
            raise HTTPException(status_code=400, detail=f"最大有效天数为{settings.MAX_DAYS}天")
        exp_time = datetime.datetime.now() + datetime.timedelta(days=value)
        exp_count = -1
    elif style == '1':
        if value < 1:
            raise HTTPException(status_code=400, detail="最小有效次数为1次")
        exp_time = datetime.datetime.now() + datetime.timedelta(days=1)
        exp_count = value
    else:
        exp_time = datetime.datetime.now() + datetime.timedelta(days=1)
        exp_count = -1
    key = uuid.uuid4().hex
    if file:
        size = await storage.get_size(file)
        if size > settings.FILE_SIZE_LIMIT:
            raise HTTPException(status_code=400, detail="文件过大")
        _text, _type, name = await storage.get_text(file, key), file.content_type, file.filename
        background_tasks.add_task(storage.save_file, file, _text)
    else:
        size, _text, _type, name = len(text), text, 'text', '文本分享'
    s.add(Codes(code=code, text=_text, size=size, type=_type, name=name, count=exp_count, exp_time=exp_time, key=key))
    await s.commit()
    upload_ip_limit.add_ip(ip)
    return {
        'detail': '分享成功，请点击我的文件按钮查看上传列表',
        'data': {'code': code, 'key': key, 'name': name}
    }


if __name__ == '__main__':
    import uvicorn

    uvicorn.run('main:app', host='0.0.0.0', port=settings.PORT, reload=settings.DEBUG)

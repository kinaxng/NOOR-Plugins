from __future__ import annotations
import asyncio
import hashlib
from pathlib import PurePosixPath
from typing import Any
import grpc
import httpx
from google.protobuf.empty_pb2 import Empty
from . import clouddrive_pb2 as pb
from . import clouddrive_pb2_grpc as rpc

class CD2Error(RuntimeError): pass

class CD2Client:
    supports_force_refresh=True
    def __init__(self, config: dict[str, Any]):
        self.endpoint=str(config.get("cd2_endpoint") or "192.168.31.10:19798").replace("http://","").replace("https://","").rstrip("/")
        self.token=str(config.get("cd2_token") or "")
        self.root=str(config.get("cd2_root") or "/dbonline/国产精选").rstrip("/")
        if not self.token: raise ValueError("请先在精选女优设置中填写 CD2 Token")
        self.metadata=(("authorization",f"Bearer {self.token}"),)
        self.channel=grpc.insecure_channel(self.endpoint,options=(("grpc.max_receive_message_length",32*1024*1024),("grpc.max_send_message_length",32*1024*1024)))
        self.stub=rpc.CloudDriveFileSrvStub(self.channel);self.paths:dict[str,str]={}
    def seed_paths(self, values:dict[str,str]):
        for fid,path in values.items():
            if fid and path:self.paths[str(fid)]=self.relative(path)
    def relative(self,path:str)->str:
        value="/"+str(path or "").replace("\\","/").strip("/")
        if value==self.root:return "/"
        if value.startswith(self.root+"/"):return value[len(self.root):]
        return value
    def canonical(self,path:str)->str:
        value="/"+str(path or "").strip("/")
        return self.root+value if value!="/" else self.root
    def _normalize(self,item)->dict[str,Any]:
        path=str(item.fullPathName or "");fid=str(item.id or "")
        if fid and path:self.paths[fid]=path
        hashes=dict(item.fileHashes)
        return {"file_id":fid,"parent_id":"","name":str(item.name or ""),"sha1":str(hashes.get(2) or "").upper(),"size":int(item.size or 0),"pick_code":fid,"is_directory":bool(item.isDirectory),"extension":PurePosixPath(str(item.name or "")).suffix.lstrip(".").lower(),"updated_at":int(item.writeTime.seconds or 0),"full_path":path}
    def _list(self,path,force=False):
        rows=[]
        for reply in self.stub.GetSubFiles(pb.ListSubFileRequest(path=path,forceRefresh=force),metadata=self.metadata,timeout=30):rows.extend(reply.subFiles)
        return [self._normalize(row) for row in rows]
    async def list_folder(self,folder_id:str,*,offset=0,limit=500,force=False):
        path=self.paths.get(str(folder_id))
        if not path:raise CD2Error(f"CD2 缓存中缺少目录路径：{folder_id}")
        rows=await asyncio.to_thread(self._list,path,False);page=rows[offset:offset+limit]
        for row in page:row["parent_id"]=str(folder_id)
        return {"items":page,"count":len(rows),"offset":offset}
    async def file_info(self,file_id:str):
        path=self.paths.get(str(file_id));
        if not path:raise CD2Error("CD2 缓存中缺少文件路径")
        parent,name=str(PurePosixPath(path).parent),PurePosixPath(path).name
        item=await asyncio.to_thread(self.stub.FindFileByPath,pb.FindFileByPathRequest(parentPath=parent,path=name),metadata=self.metadata,timeout=20)
        return self._normalize(item)
    async def create_folder(self,parent_id,name):
        parent=self.paths[str(parent_id)]
        result=await asyncio.to_thread(self.stub.CreateFolder,pb.CreateFolderRequest(parentPath=parent,folderName=name),metadata=self.metadata,timeout=30)
        if not result.result.success:raise CD2Error(result.result.errorMessage)
        return self._normalize(result.folderCreated)
    async def rename_file(self,file_id,name):
        source=self.paths[str(file_id)]
        result=await asyncio.to_thread(self.stub.RenameFile,pb.RenameFileRequest(theFilePath=source,newName=name),metadata=self.metadata,timeout=30)
        if not result.success:raise CD2Error(result.errorMessage)
        self.paths[str(file_id)]=str(PurePosixPath(source).with_name(name));return {"file_name":name}
    async def move_file(self,file_id,parent_id):
        result=await asyncio.to_thread(self.stub.MoveFile,pb.MoveFileRequest(theFilePaths=[self.paths[str(file_id)]],destPath=self.paths[str(parent_id)],conflictPolicy=pb.MoveFileRequest.Overwrite),metadata=self.metadata,timeout=60)
        if not result.success:raise CD2Error(result.errorMessage)
        return result
    async def copy_file(self,file_id,parent_id,*,no_duplicate=True):
        policy=pb.CopyFileRequest.Skip if no_duplicate else pb.CopyFileRequest.Overwrite
        result=await asyncio.to_thread(self.stub.CopyFile,pb.CopyFileRequest(theFilePaths=[self.paths[str(file_id)]],destPath=self.paths[str(parent_id)],conflictPolicy=policy),metadata=self.metadata,timeout=60)
        if not result.success:raise CD2Error(result.errorMessage)
        return result
    async def delete_file(self,file_id,parent_id):
        result=await asyncio.to_thread(self.stub.DeleteFile,pb.FileRequest(path=self.paths[str(file_id)]),metadata=self.metadata,timeout=30)
        if not result.success:raise CD2Error(result.errorMessage)
        return result
    async def upload_bytes(self,folder_id,name,content:bytes):
        parent=self.paths[str(folder_id)];path=str(PurePosixPath(parent)/name)
        channel=grpc.aio.insecure_channel(self.endpoint,options=(("grpc.max_receive_message_length",32*1024*1024),("grpc.max_send_message_length",32*1024*1024)))
        stub=rpc.CloudDriveFileSrvStub(channel);stream=None
        try:
            device=(await stub.GetMachineId(Empty(),metadata=self.metadata,timeout=10)).result
            stream=stub.RemoteUploadChannel(pb.RemoteUploadChannelRequest(device_id=device),metadata=self.metadata)
            started=await stub.StartRemoteUpload(pb.StartRemoteUploadRequest(file_path=path,file_size=len(content),known_hashes={1:hashlib.md5(content).hexdigest(),2:hashlib.sha1(content).hexdigest()},client_can_calculate_hashes=True),metadata=self.metadata,timeout=20)
            upload_id=started.upload_id
            async with asyncio.timeout(180):
                async for message in stream:
                    if message.upload_id!=upload_id:continue
                    kind=message.WhichOneof("request")
                    if kind=="read_data":
                        request=message.read_data;chunk=content[request.offset:request.offset+request.length]
                        await stub.RemoteReadData(pb.RemoteReadDataUpload(upload_id=upload_id,offset=request.offset,length=len(chunk),lazy_read=request.lazy_read,data=chunk,is_last_chunk=request.offset+len(chunk)>=len(content)),metadata=self.metadata,timeout=30)
                    elif kind=="hash_data":
                        request=message.hash_data;algorithm=hashlib.md5 if request.hash_type==1 else hashlib.sha1
                        await stub.RemoteHashProgress(pb.RemoteHashProgressUpload(upload_id=upload_id,bytes_hashed=len(content),total_bytes=len(content),hash_type=request.hash_type,hash_value=algorithm(content).hexdigest()),metadata=self.metadata,timeout=30)
                    elif kind=="status_changed":
                        status=message.status_changed
                        if status.status in {pb.UploadFileInfo.Finish,pb.UploadFileInfo.Skipped}:break
                        if status.status in {pb.UploadFileInfo.Error,pb.UploadFileInfo.FatalError,pb.UploadFileInfo.Cancelled}:
                            raise CD2Error(status.error_message or "CD2 上传失败")
            return {"uploaded":True,"name":name}
        finally:
            if stream:stream.cancel()
            await channel.close()
    async def download_bytes(self,item:dict[str,Any],*,max_bytes:int):
        path=str(item.get("full_path") or self.paths.get(str(item.get("file_id"))) or "")
        if not path:raise CD2Error("CD2 缓存中缺少文件路径")
        deadline=asyncio.get_running_loop().time()+60
        async with httpx.AsyncClient(timeout=60,follow_redirects=True) as client:
            while True:
                info=await asyncio.to_thread(self.stub.GetDownloadUrlPath,pb.GetDownloadUrlPathRequest(path=path,preview=False,lazy_read=True,get_direct_url=False),metadata=self.metadata,timeout=20)
                url=info.directUrl or info.downloadUrlPath.replace("{SCHEME}","http").replace("{HOST}",self.endpoint).replace("{PREVIEW}","false")
                if url.startswith("/"): url=f"http://{self.endpoint}{url}"
                response=await client.get(url,headers=dict(info.additionalHeaders))
                if response.status_code!=502 or asyncio.get_running_loop().time()>=deadline:
                    response.raise_for_status();raw=response.content;break
                await asyncio.sleep(1)
        if len(raw)>max_bytes:raise ValueError("CD2 文件超过允许读取大小")
        return raw

    async def watch_changes(self):
        channel=grpc.aio.insecure_channel(self.endpoint,options=(("grpc.max_receive_message_length",32*1024*1024),))
        stub=rpc.CloudDriveFileSrvStub(channel)
        try:
            stream=stub.PushMessage(Empty(),metadata=self.metadata)
            async for message in stream:
                if message.messageType != pb.CloudDrivePushMessage.FILE_SYSTEM_CHANGE:
                    continue
                change=message.fileSystemChange
                yield {"type":pb.FileSystemChange.ChangeType.Name(change.changeType).lower(),"path":self.canonical(change.path),"new_path":self.canonical(change.newPath) if change.HasField("newPath") else "","is_directory":bool(change.isDirectory)}
        finally:
            await channel.close()

import requests
import datetime
import json
import random
import signal
import time
import traceback
import urllib.parse
import warnings
from concurrent.futures import CancelledError, ThreadPoolExecutor
from queue import Queue
from .buffer import Buffer
from ..parser.replay import Parser
from .. import config
from ..exceptions  import ChatParseException,IllegalFunctionCall
from ..paramgen    import arcparam
from ..processors.default.processor import DefaultProcessor
from ..processors.combinator import Combinator

logger = config.logger(__name__)
headers = config.headers
MAX_RETRY = 10


class ReplayChat:
    '''
    ### -----------------------------------------------------------
    ### [Warning] ReplayChat is integrated into LiveChat.
    ### This class is deprecated and will be removed at v0.0.5.0.
    ### ReplayChatはLiveChatに統合しました。
    ### このクラスはv0.0.5.0で廃止予定です。
    ### -----------------------------------------------------------

     スレッドプールを利用してYouTubeのライブ配信のチャットデータを取得する

    Parameter
    ---------
    video_id : str
        動画ID

    seektime : int
        リプレイするチャットデータの開始時間（秒）

    processor : ChatProcessor
        チャットデータを加工するオブジェクト

    buffer : Buffer(maxsize:20[default])
        チャットデータchat_componentを格納するバッファ。
        maxsize : 格納できるchat_componentの個数
        default値20個。1個で約5~10秒分。

    interruptable : bool
        Ctrl+Cによる処理中断を行うかどうか。

    callback : func
        _listen()関数から一定間隔で自動的に呼びだす関数。

    done_callback : func
        listener終了時に呼び出すコールバック。

    direct_mode : bool
        Trueの場合、bufferを使わずにcallbackを呼ぶ。
        Trueの場合、callbackの設定が必須
        (設定していない場合IllegalFunctionCall例外を発生させる）  

    Attributes
    ---------
    _executor : ThreadPoolExecutor
        チャットデータ取得ループ（_listen）用のスレッド

    _is_alive : bool
        チャット取得を停止するためのフラグ
    '''

    _setup_finished = False

    #チャット監視中のListenerのリスト
    _listeners= []

    def __init__(self, video_id,
                seektime = 0,
                processor = DefaultProcessor(),
                buffer = None,
                interruptable = True,
                callback = None,
                done_callback = None,
                direct_mode = False
                ):

        warnings.warn(""
        f"\n{'-'*60}\n[WARNING] ReplayChat is integrated into LiveChat.\n"
        f"{' '*5}This is deprecated and will be removed at v0.0.5.0.\n"
        f"{'-'*60}\n"
        )
        self.video_id  = video_id
        self.seektime = seektime
        if isinstance(processor, tuple):
            self.processor = Combinator(processor)
        else:
            self.processor = processor
        self._buffer = buffer
        self._callback = callback
        self._done_callback = done_callback
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._direct_mode = direct_mode
        self._is_alive   = True
        self._parser = Parser()
        self._pauser = Queue()
        self._pauser.put_nowait(None)
       
        self._setup()

        if not ReplayChat._setup_finished:
            ReplayChat._setup_finished = True
            if interruptable:
                signal.signal(signal.SIGINT,  (lambda a, b:  
                (ReplayChat.shutdown(None,signal.SIGINT,b))
                ))
        ReplayChat._listeners.append(self)

    def _setup(self):
        #direct modeがTrueでcallback未設定の場合例外発生。
        if self._direct_mode:
            if self._callback is None:
                raise IllegalFunctionCall(
                    "direct_mode=Trueの場合callbackの設定が必須です。")
        else:
            #direct modeがFalseでbufferが未設定ならばデフォルトのbufferを作成
            if self._buffer is None:
                self._buffer = Buffer(maxsize = 20)
                #callbackが指定されている場合はcallbackを呼ぶループタスクを作成
            if self._callback is None:
                pass 
            else:
                #callbackを呼ぶループタスクの開始
                self._executor.submit(self._callback_loop,self._callback)
        #_listenループタスクの開始
        listen_task = self._executor.submit(self._startlisten)
        #add_done_callbackの登録
        if self._done_callback is None:
            listen_task.add_done_callback(self.finish)
        else:
            listen_task.add_done_callback(self._done_callback)

    def _startlisten(self):
        """最初のcontinuationパラメータを取得し、
        _listenループのタスクを作成し開始する
        """
        initial_continuation = self._get_initial_continuation()
        if initial_continuation is None:
            self.terminate()
            logger.debug(f"[{self.video_id}]No initial continuation.")
            return
        self._listen(initial_continuation)
   
    def _get_initial_continuation(self):
        ''' チャットデータ取得に必要な最初のcontinuationを取得する。'''
        try:    
            initial_continuation = arcparam.getparam(self.video_id,self.seektime)
        except ChatParseException as e:
            self.terminate()
            logger.debug(f"[{self.video_id}]Error:{str(e)}")
            return
        except KeyError:
            logger.debug(f"[{self.video_id}]KeyError:"
                         f"{traceback.format_exc(limit = -1)}")
            self.terminate()
            return
        return initial_continuation

    def _listen(self, continuation):
        ''' continuationに紐付いたチャットデータを取得し
        BUfferにチャットデータを格納、
        次のcontinuaitonを取得してループする

        Parameter
        ---------
        continuation : str
            次のチャットデータ取得に必要なパラメータ
        '''
        try:
            with requests.Session() as session:
                while(continuation and self._is_alive):
                    if self._pauser.empty():
                        #pause
                        self._pauser.get()
                        #resume
                        #prohibit from blocking by putting None into _pauser.
                        self._pauser.put_nowait(None)
                    livechat_json = (
                      self._get_livechat_json(continuation, session, headers)
                    )
                    metadata, chatdata =  self._parser.parse( livechat_json )
                    timeout = metadata['timeoutMs']/1000
                    chat_component = {
                        "video_id" : self.video_id,
                        "timeout"  : timeout,
                        "chatdata" : chatdata
                    }
                    time_mark =time.time()
                    if self._direct_mode:
                        self._callback(
                            self.processor.process([chat_component])
                            )
                    else:
                        self._buffer.put(chat_component)
                    diff_time = timeout - (time.time()-time_mark)
                    if diff_time < 0 : diff_time=0
                    time.sleep(diff_time)        
                    continuation = metadata.get('continuation')  
        except ChatParseException as e:
            self.terminate()
            logger.error(f"{str(e)}（video_id:\"{self.video_id}\"）")
            return            
        except (TypeError , json.JSONDecodeError) :
            self.terminate()
            logger.error(f"{traceback.format_exc(limit = -1)}")
            return
        
        logger.debug(f"[{self.video_id}]チャット取得を終了しました。")

    def _get_livechat_json(self, continuation, session, headers):
        '''
        チャットデータが格納されたjsonデータを取得する。
        '''
        continuation = urllib.parse.quote(continuation)
        livechat_json = None
        status_code = 0
        url =(
            f"https://www.youtube.com/live_chat_replay/get_live_chat_replay?"
            f"continuation={continuation}&pbj=1")
        for _ in range(MAX_RETRY + 1):
            with session.get(url ,headers = headers) as resp:
                try:
                    text = resp.text
                    status_code = resp.status_code
                    livechat_json = json.loads(text)
                    break
                except json.JSONDecodeError :
                    time.sleep(1)
                    continue
        else:
            logger.error(f"[{self.video_id}]"
                    f"Exceeded retry count. status_code={status_code}")
            self.terminate()
            return None
        return livechat_json
  
    def _callback_loop(self,callback):
        """ コンストラクタでcallbackを指定している場合、バックグラウンドで
        callbackに指定された関数に一定間隔でチャットデータを投げる。        
        
        Parameter
        ---------
        callback : func
            加工済みのチャットデータを渡す先の関数。
        """
        while self.is_alive():
            items = self._buffer.get()
            data = self.processor.process(items)
            callback(data)

    def get(self):
        """ bufferからデータを取り出し、processorに投げ、
        加工済みのチャットデータを返す。
        
        Returns
             : Processorによって加工されたチャットデータ
        """
        if self._callback is None:
            items = self._buffer.get()
            return  self.processor.process(items)
        raise IllegalFunctionCall(
            "既にcallbackを登録済みのため、get()は実行できません。")

    def pause(self):
        if not self._pauser.empty():
            self._pauser.get()

    def resume(self):
        if self._pauser.empty():
            self._pauser.put_nowait(None)
        

    def is_alive(self):
        return self._is_alive

    def finish(self,sender):
        '''Listener終了時のコールバック'''
        try: 
            self.terminate()
        except RuntimeError:
            logger.debug(f'[{self.video_id}]cancelled:{sender}')

    def terminate(self):
        '''
        Listenerを終了する。
        '''
        self._is_alive = False
        if self._direct_mode == False:
            #bufferにダミーオブジェクトを入れてis_alive()を判定させる
            self._buffer.put({'chatdata':'','timeout':1}) 
        logger.info(f'[{self.video_id}]終了しました')
  
    @classmethod
    def shutdown(cls, event, sig = None, handler=None):
        logger.debug("シャットダウンしています")
        for t in ReplayChat._listeners:
            t._is_alive = False
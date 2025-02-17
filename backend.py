# -*- coding: utf-8 -*-
import hashlib
import hmac
import random, pickle
import time
import uuid
from lzpy import LZString
from flask import Flask, redirect, url_for, render_template, request, jsonify
from flask_cors import CORS, cross_origin
from flask_limiter import Limiter
from apscheduler.schedulers.background import BackgroundScheduler
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from orm import Archive, DB_Init, dump_vote_records
from model import VoteRecordDB
from utils import ThreadSafeOrderedDict, get_client_ip
import atexit


app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

# 1.4.0 服务器逻辑修改，唯一的服务器就是localhost
from config import ProductionConfig as Config

# 限制用户访问流量
limiter = Limiter(
    key_func=get_client_ip,  # 根据请求的源IP地址来限制
    default_limits=[f"{Config.IP_LIMITER_PER_DAY} per day", f"{Config.IP_LIMITER_PER_HOUR} per hour"]
)
limiter.init_app(app)

@app.route('/', methods=['GET'])
@cross_origin()
def page():
    return render_template('page.html')

# 创建后台调度器实例
scheduler = BackgroundScheduler()

# 全局变量
mem_db:VoteRecordDB = DB_Init()
operators_id_dict = Config.DICT_NAME
operators_id_dict_length = len(operators_id_dict)
operators_name_list = list(operators_id_dict.keys())

# 存储选票id
# TODO: 没有过期时间，很容易爆内存的，而且不安全
# TODO: Lock -> MQ
ballot_id_set = set()
ballot_id_set_Lock = Lock()


# WARNING: 投票数据安全性不能保证，在进行写入数据库前不能确保数据的安全
# 如果mem_db 非空，那么将mem_db的内容更新到数据库中
# TODO: Lock -> mq
def _process_score(id, scores, locks):
    # with locks[id]: # 不需要强一致性
    tmp_val = scores[id]
    # ...
    return (tmp_val, id)

def _process_scores_concurrently(scores, locks):
    result_list = []
    # 测试了一下，好像并行更慢，才106个数据确实没必要并发优化
    for id in range(len(locks)):
        result = _process_score(id, scores, locks)
        if result is not None:
            result_list.append(result)
    return result_list

# 添加作业到调度器，每隔Config.OPERATORS_VOTE_RECORDS_DB_DUMP_INTERVAL分钟执行一次Memory_DB_Dump函数
# 持久化投票分数到OPERATORS_VOTE_RECORDS_DB
# WARNING: 此处出现竞态，不过确保最终一致就行，没必要强一致，所以就不加锁了
@scheduler.scheduled_job('interval', minutes=Config.OPERATORS_VOTE_RECORDS_DB_DUMP_INTERVAL)
def Memory_DB_Dump():
    win_list =  _process_scores_concurrently(mem_db.score_win, mem_db.lock_score_win)
    lose_list = _process_scores_concurrently(mem_db.score_lose, mem_db.lock_score_lose)
    app.logger.info("dump_vote_records start.")
    dump_vote_records(win_list, lose_list, mem_db.operators_vote_matrix)
    app.logger.info("dump_vote_records fin.")

@app.route('/new_compare', methods=['POST'])
@cross_origin()
def new_compare():
    # 由于本次投票开放时间不长，参与投票的六星干员从头到尾固定。
    # 为提升性能，取消了“之前抽取次数少的干员优先抽取”的功能，该功能可在1.0.4版本中找到。
    len_lst_name_1 = operators_id_dict_length - 1
    a = random.randint(0, len_lst_name_1)
    b = random.randint(0, len_lst_name_1)
    while a == b:
        b = random.randint(0, len_lst_name_1)
    result = compare(a, b)
    return result


@app.route('/save_score', methods=['POST']) 
@cross_origin()
def save_score():
     # code不对，请求非法，verify() == 0
     # code对，此ip投票 <= 50 次，verify() == 1
     # code对，此ip投票 > 50 次，每票权重降为0.01票，verify() == 0.01
    if not request.is_json:
        return '', 400
    win_name = request.get_json().get('win_name', None) # request.args.get('win_name')
    lose_name = request.get_json().get('lose_name', None) # request.args.get('lose_name')
    code = request.get_json().get('code', None) # request.args.get('code')
    
    if not win_name or not lose_name or not code:
        return '', 400
    if win_name not in operators_id_dict or lose_name not in operators_id_dict:
        return '', 400
    vrf = verify_code(code, win_name, lose_name)
    if vrf is None:
        return '', 429
    elif vrf:
        win_operator_id = operators_id_dict[win_name]
        lose_operator_id = operators_id_dict[lose_name]
        with mem_db.lock_score_win[win_operator_id]:
            mem_db.score_win[win_operator_id] += vrf
            mem_db.operators_vote_matrix[win_operator_id][lose_operator_id] += vrf
        with mem_db.lock_score_lose[lose_operator_id]:
            mem_db.score_lose[lose_operator_id] += vrf
            mem_db.operators_vote_matrix[lose_operator_id][win_operator_id] -= vrf
    return 'success'

@app.route('/view_final_order', methods=['GET'])
@cross_origin()
def view_final_order():
    # lst_rate 计算胜率，公式是 (胜利分数 / (胜利分数 + 失败分数)) * 100
    # lst_score 计算净胜分，公式是 胜利分数 - 失败分数。
    # TODO: 简化代码逻辑，把sort丢到前端去
    # TODO: 修复除以0的bug
    lst_win_score = list(mem_db.score_win.values())
    lst_lose_score = list(mem_db.score_lose.values())
    
    lst_rate = [100 * lst_win_score[_] / (lst_win_score[_] + lst_lose_score[_]) for _ in range(len(lst_win_score))]
    lst_score = [lst_win_score[_] - lst_lose_score[_] for _ in range(len(lst_win_score))]
    dict_score = dict(zip(zip(operators_name_list, lst_score), lst_rate))

    final_n_s, final_rate = zip(*sorted(dict_score.items(), key=lambda _: -_[1]))
    final_name, final_score = zip(*final_n_s)
    final_score = ['%.2f'%_ for _ in final_score]
    final_rate = ['%.1f'%_ + ' %' for _ in final_rate]
    return jsonify({'name': final_name, 'rate': final_rate, 'score': final_score, 'count': '已收集数据 ' + '%.2f'%(sum(lst_win_score)) + ' 条'})

@app.route('/upload', methods=['POST'])
@cross_origin()
def upload():
    data = request.get_json()
    key = data.get('key')
    result = data.get('data')
    vote_times = int(data.get('vote_times'))
    ip = get_client_ip()
    is_create = False
    if not result:
        return jsonify({'error': 'result is required'})
    if not key:
        is_create = True
        timestamp = str(time.time())
        key = hmac.new(ip.encode(), timestamp.encode(), hashlib.sha1).hexdigest()
    result = LZString.decompressFromUTF16(result)
    archive = Archive(key = key, data = result, ip = ip, vote_times = vote_times)
    archive.save(force_insert=is_create)
    return jsonify({'key': key, "updated_at": int(archive.updated_at.timestamp())}), 200

@app.route('/sync', methods=['GET'])
@cross_origin()
def sync():
    key = request.args.get('key')
    if not key:
        return jsonify({'error': '未填写秘钥'})
    if len(key) != 40:
        return jsonify({'error': '秘钥长度不合法'})
    try:
        archive = Archive.get(Archive.key == key)
    except Archive.DoesNotExist:
        return jsonify({'error': '秘钥不存在'})
    result = LZString.compressToUTF16(archive.data)
    return jsonify({'data': result, "vote_times": archive.vote_times, "updated_at": archive.updated_at})

@app.route('/get_operators_1v1_matrix', methods=['POST'])
@cross_origin()
@limiter.limit("600 per hour")
def get_operators_1v1_matrix():
    # 没必要强一致性，最终一致就行
    return jsonify({"operators_1v1_matrix": mem_db.operators_vote_matrix})

# 流量控制返回结果
@app.errorhandler(429)
def handle_rate_limit_exceeded(e):
    return jsonify({"error": "请求频率超过限制", 'code':400}), 200

def compare(a:int, b:int):
    # 存在一致性问题，仍然不确保code_random会不会撞
    code_random = uuid.uuid4().int
    code = code_random + a + b
    if ballot_id_set_Lock.acquire(timeout=4):
        ballot_id_set.add(code)
        ballot_id_set_Lock.release()
    else:
        app.logger.error("ballot_id_set.add 超时")
    return operators_name_list[a] + ' ' + operators_name_list[b] + ' ' + str(code_random)


def verify_ip():
    # code不对，请求非法，verify() == 0
    # code对，此ip投票 <= 50 次，verify() == 1
    # code对，此ip投票 > 50 次，每票权重降为0.01票，verify() == 0.01
    # ...存在严重的一致性问题
    client_ip = get_client_ip()
    with open('ip/ip_ban.pickle', 'rb') as f:
        ip_ban = pickle.load(f)
    if client_ip in ip_ban:
        return 0.01

    with open('ip/ip_dict.pickle', 'rb') as f:
        ip_dict = pickle.load(f)
    if not client_ip in ip_dict:
        ip_dict[client_ip] = 0

    ip_dict[client_ip] += 1

    if ip_dict[client_ip] > 50:
        del ip_dict[client_ip]
        ip_ban.add(client_ip)

        with open('ip/ip_dict.pickle', 'wb') as f:
            pickle.dump(ip_dict, f)
        with open('ip/ip_ban.pickle', 'wb') as f:
            pickle.dump(ip_ban, f)
        return 0.01
    else:
        with open('ip/ip_dict.pickle', 'wb') as f:
            pickle.dump(ip_dict, f)
        return 1

# TODO: Lock -> MQ
def verify_code(code, win_name, lose_name):
    # code不对，请求非法，verify() == 0
    # code对，此ip投票 <= 50 次，verify() == 1
    # code对，此ip投票 > 50 次，每票权重降为0.01票，verify() == 0.01
    code = int(code) + operators_id_dict[win_name] + operators_id_dict[lose_name]
    if ballot_id_set_Lock.acquire(timeout=5):
        if code in ballot_id_set:
            ballot_id_set.remove(code)
            ballot_id_set_Lock.release()
            return verify_ip()
        else:
            ballot_id_set_Lock.release()
            return 0
    else:
        app.logger.info(f"verify_code fail. ballot_id_set_Lock acquire fail.")
        return None
    
# 启动调度器
scheduler.start()

# 处理termination信号
def handle_exit_signal():
    app.logger.info("Received termination signal. Dumping vote records and shutting down.")
    Memory_DB_Dump()
    scheduler.shutdown()
    app.logger.info("Scheduler shut down. Exiting application.")
atexit.register(handle_exit_signal)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9876)
"""Small reproducible Chinese retrieval check; synthetic, not business acceptance."""

import json
import resource
import time

from service import MODEL_ID, Encoder

corpus = [
    "删除用户权限后，系统要先归档未完成的修改，然后清理设备上的受管框架。",
    "腾讯云服务器每天定时备份PostgreSQL，恢复演练验证数据完整。",
    "Web登录使用邮箱验证码验证新设备，未绑定邮箱的成员由超级管理员审批。",
    "不同开发工具认领任务时使用排他租约，避免两名开发者修改同一批代码。",
    "审查支付接口幂等请求，禁止重复扣款，保留transaction_id。",
    "蓝色按钮使用圆角和白色字体。",
    "长期接口约定必须引用原文，经过独立审核后才成为生效知识。",
    "保护路径禁止修改，只有授权管理员变更任务范围后才能重新准备。",
    "桌面程序核验升级包的签名与摘要，在线下载后重启进入新版本。",
    "共享文档只索引指定Git目录，代码仓库按成员获准的仓库分别读取。",
]
cases = [
    ("员工被踢出后怎么交接并卸载", 0),
    ("数据库灾难恢复", 1),
    ("换电脑怎么验证身份", 2),
    ("两个人同时抢同一项工作", 3),
    ("transaction_id重复请求付款", 4),
    ("界面按钮外观", 5),
    ("同事总结的经验什么时候正式可用", 6),
    ("哪些文件绝对不能动", 7),
    ("PC应用如何自动升级", 8),
    ("只能访问被授予权限的Git代码", 9),
    ("离职时保全尚未交付的工作", 0),
    ("长期记忆需别人审查来源", 6),
]

start = time.monotonic()
encoder = Encoder("/model")
load_seconds = time.monotonic() - start
vectors = [encoder.encode(value, "passage")[0] for value in corpus]
results = []
for query, expected in cases:
    start = time.monotonic()
    vector = encoder.encode(query, "query")[0]
    scores = [sum(a * b for a, b in zip(vector, other)) for other in vectors]
    ranking = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    results.append(
        {
            "query": query,
            "expected": expected,
            "top3": ranking[:3],
            "rank": ranking.index(expected) + 1,
            "seconds": round(time.monotonic() - start, 4),
        }
    )
long = encoder.encode(corpus[0] * 50, "passage")
result = {
    "model": MODEL_ID,
    "synthetic": True,
    "corpus_size": len(corpus),
    "queries": len(cases),
    "top1": sum(item["rank"] == 1 for item in results),
    "top3": sum(item["rank"] <= 3 for item in results),
    "load_seconds": round(load_seconds, 3),
    "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
    "long_windows": long[1],
    "long_dimensions": len(long[0]),
    "results": results,
}
print(json.dumps(result, ensure_ascii=False))
assert result["top3"] / len(cases) >= 0.9

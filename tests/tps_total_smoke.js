// TPS 状态栏「出」标签 + 「会话总计」段冒烟测试（0.6.14）。
//
// 为什么需要它：本次改动把本轮输出的显示文案从 `out` 换成中文缩写「出」，
// 并在会话累计组末尾新增「会话总计 xx」段（本对话总计消耗 Token）。
// 这类改动最容易出的岔子不是写错表达式，而是**悄悄退回旧形态**——
// 标签又变回 `out`、总计段没进渲染路径、聚合口径写反（漏了 totalTokens 优先）、
// 或忘了把新字段放进内容签名（于是数值变化根本不重绘）。语法照样合法、
// `node --check` 照样通过，肉眼 review 极易漏掉。这里锁四件事：
//   ① 本轮输出段文案必须是「出」，不得回退成 `out `；
//   ② 「会话总计」段存在、挂在会话累计组（p:7, g:1）、经 fmtTok 格式化；
//   ③ 聚合口径：优先服务端 totalTokens，缺失时退回 input+输出估算，
//      且 agg.total 必须进内容签名；
//   ④ fmtTok 的 k/m 缩写行为（含 >=10k 取整，这是文档示例容易写错的点）。
//
// 用法：node tests/tps_total_smoke.js skills/zcode-tokenspeed/scripts/zcode-tps.js
// 退出码 0 = 通过；1 = 有断言失败；2 = 用法/读取错误。
const fs = require("fs");

const target = process.argv[2];
if (!target) {
  console.error("用法：node tests/tps_total_smoke.js <zcode-tps.js 的路径>");
  console.error("例如：node tests/tps_total_smoke.js skills/zcode-tokenspeed/scripts/zcode-tps.js");
  process.exit(2);
}
let src;
try {
  src = fs.readFileSync(target, "utf8");
} catch (err) {
  console.error("读不到被测脚本：" + target);
  console.error("  " + err.message);
  process.exit(2);
}

const failures = [];
const check = (ok, label, extra) => {
  if (ok) return;
  failures.push(label + (extra ? "  —— " + extra : ""));
};

// ---------- ① 本轮输出段：文案必须是「出」 ----------
check(/span\("出 "\)/.test(src), "本轮输出段文案不是「出」（退回 out 了？）");
check(!/span\("out "\)/.test(src), '源码里仍有 span("out ")：中文缩写没改干净');

// ---------- ② 会话总计段：存在、挂 g:1、走 fmtTok ----------
check(/span\("会话总计 "\)/.test(src), "找不到「会话总计」段");
check(/\{\s*p:\s*7,\s*g:\s*1,[^}]*会话总计/.test(src),
      "「会话总计」段没有挂在会话累计组（p:7, g:1）");
check(/会话总计[^}]*fmtTok\(agg\.total\)/.test(src),
      "「会话总计」段没有经 fmtTok(agg.total) 格式化");

// ---------- ③ 聚合口径与刷新签名 ----------
check(/const agg = \{[^}]*total:\s*0[^}]*\}/.test(src),
      "agg 聚合对象缺少 total 字段初始化");
check(/agg\.total \+= t\.totalTokens > 0 \? t\.totalTokens :/.test(src),
      "agg.total 未以「服务端 totalTokens 优先」的口径聚合");
check(/agg\.rounds,\s*agg\.input,\s*agg\.cache,\s*agg\.output,\s*agg\.total\]\.join/.test(src),
      "agg.total 未进内容签名（数值变化不会触发重绘）");

// ---------- ④ fmtTok 行为（k/m 缩写，含 >=10k 取整）----------
function extractFn(name) {
  const head = "const " + name + " = ";
  const i = src.indexOf(head);
  if (i < 0) return null;
  const braceStart = src.indexOf("{", i + head.length);
  if (braceStart < 0) return null;
  let depth = 0;
  for (let j = braceStart; j < src.length; j++) {
    const c = src[j];
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) return src.slice(i + head.length, j + 1).trim();
    }
  }
  return null;
}

const fnSrc = extractFn("fmtTok");
check(!!fnSrc, "源码里找不到 `const fmtTok = ...`（函数被改名或删除了？）");
if (fnSrc) {
  let fmtTok = null;
  try {
    // eslint-disable-next-line no-eval
    fmtTok = eval("(" + fnSrc + ")");
  } catch (err) {
    check(false, "fmtTok 源码无法求值", err.message);
  }
  if (typeof fmtTok === "function") {
    const cases = [
      // [输入, 期望, 说明]
      [0, "0", "零"],
      [410, "410", "千以下原样"],
      [999, "999", "千以下边界"],
      [1000, "1k", "整千"],
      [1234, "1.2k", "不足 10k 保留一位小数"],
      [12300, "12k", "★ >=10k 取整（不是 12.3k）"],
      [45200, "45k", "★ >=10k 取整"],
      [57500, "58k", "★ >=10k 四舍五入取整"],
      [1234567, "1.2m", "百万级"],
    ];
    for (const [input, want, why] of cases) {
      let got;
      try {
        got = fmtTok(input);
      } catch (err) {
        check(false, "fmtTok(" + input + ") 抛错", err.message);
        continue;
      }
      check(got === want, "fmtTok(" + input + ") 期望 " + want + " 实得 " + got, why);
    }
  }
}

// ---------- 汇报 ----------
if (failures.length) {
  console.error("FAIL " + failures.length + " 项：");
  failures.forEach((f, i) => console.error("  " + (i + 1) + ". " + f));
  process.exit(1);
}
console.log("OK 会话总计段 + 「出」标签冒烟通过（文案 / 挂载 / 口径 / 签名 + fmtTok 缩写）");

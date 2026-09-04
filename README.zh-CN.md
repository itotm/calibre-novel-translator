[English](README.md) · __简体中文__

---

# 电子书翻译器（Novel）—— Calibre 插件

![Ebook Translator Calibre Plugin](images/logo.png)

一个可以将电子书翻译成指定语言（原文译文对照）的 Calibre 插件。

> **这是一个分支版本**，源自 [bookfere/Ebook-Translator-Calibre-Plugin](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin)。
> 插件本身的功劳归上游作者所有，本仓库只增加了下列内容。它使用独立的插件名称、
> 配置文件和缓存目录，因此可以与官方插件**并存安装**，两者互不干扰。

![Translation illustration](images/sample-sc.png)

---

## 本分支新增的内容

### 小说模式（Novel Mode）

在「高级模式」和「批量模式」之外新增的第三种翻译模式，专为长篇叙事内容设计。
它不再逐段发送请求，而是逐章翻译，并让模型始终了解全书已经发生的内容：

* 每翻译完一章就重建**滚动摘要**与**动态术语表**（人物、地点、物品），并注入
  到后续的每一次请求中，从而在全书范围内保持人名、代词、语体和术语一致。
* 分块采用**双重上限**——token 预算与段落数量，任一达到即结束当前块，因此段落
  很短时（对白、目录列表）模型也不会丢失对齐标记。
* **滑动窗口重叠**会把上一块末尾的若干段译文作为上下文重放（标明无需重译），
  保持跨块的对话线索与代词指代。
* 引擎支持时使用**结构化 JSON 输出**，段落对齐由服务端强制保证，而不再依赖
  提示词的自觉。
* 每章结束即写入翻译缓存，中断后可从断点**续译**；最终成书直接由缓存生成，
  不会再次调用大模型。
* 章节边界可取自目录一级、目录二级（适用于一级条目为整本书的合集），或每个
  XHTML 文件算一章；并可按字数过滤封面、书名页等前置内容。

小说模式来自 [BiG86（Simone Norcini）](https://github.com/BiG86) 提交的
[PR #590](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/pull/590)，
该 PR 在上游尚未合并，此处先行合入。

### OpenRouter 翻译引擎

[OpenRouter](https://openrouter.ai) 通过一个兼容 OpenAI 的接口代理数百个模型。
除常规的提示词、模型和采样设置外，本引擎还提供该网关特有的参数。所有参数的
默认值都是中性的，即**不写入请求**，让上游供应商继续使用自己的默认值——
OpenRouter 对未提供的参数不会代为填充默认值。

| 分类 | 设置项 |
|---|---|
| 推理 | `effort`（默认 `minimal`）、`max_tokens` 预算、`exclude` |
| 采样 | `top_k`、`min_p`、`top_a` |
| 惩罚 | `frequency_penalty`、`presence_penalty`、`repetition_penalty` |
| 上限 | `max_tokens`、`seed` |
| 供应商路由 | `only`、`order`、`ignore`、`quantizations`、`sort`、`data_collection`、`allow_fallbacks`、`require_parameters`、`zdr` |
| 归属标识 | `HTTP-Referer`、`X-Title` |
| 兜底入口 | 以 JSON 形式追加的请求头与请求体字段 |

两个兜底入口在最后合并进请求，因此界面未暴露的内容（`logit_bias`、`stop`、
`transforms`、`plugins`、缓存控制、自定义会话请求头等）仍可发送。JSON 格式
有误时会在设置窗口中标红，并在发送请求时忽略，而不会中断翻译。

### OpenAI 兼容引擎的推理强度可配置

`reasoning_effort` 不再写死，而是成为 ChatGPT、Azure ChatGPT、DeepSeek 以及
任何自定义 OpenAI 兼容接口的引擎选项。保持「默认」会完全不发送该字段，这正是
普通 OpenAI 模型所期待的；`none` 用于抑制某些本地服务在每次回答前产生的推理
token（例如通过 Ollama 运行的 Gemma）；其余档位则是有意花费推理 token。小说
模式同样遵循该设置，因此推理模型可以一边思考，一边被强制以 JSON 作答。

### 本分支修复的问题

* 结构化输出路径在请求体中强制 `stream: true`，却没有同步告知引擎，导致关闭
  流式响应的引擎会把原始 SSE 数据当作 JSON 解析。
* OpenRouter 会把部分上游供应商的故障放在 HTTP 200 的响应体里；现在会给出
  真正的错误信息，而不是含糊的解析失败。
* 小说模式分支遗留的测试预期已与代码对齐（`keepalive` 请求参数、结构化输出
  的请求体）。

---

## 与官方插件并存安装

本分支以 **Ebook Translator (Novel)** 的身份注册，并保存自己的状态，因此与
官方插件的安装之间不共享任何内容：

| | 官方版 | 本分支 |
|---|---|---|
| 插件名称 | Ebook Translator | Ebook Translator (Novel) |
| 导入名称 | `ebook_translator` | `ebook_translator_novel` |
| 配置文件 | `plugins/ebook_translator.json` | `plugins/ebook_translator_novel.json` |
| 缓存目录 | `…EbookTranslator` | `…EbookTranslator.Novel` |

本分支不使用持续集成：发布用的压缩包在本地构建，与日常安装用的是同一个脚本。

```sh
./build_plugin.sh
```

它会生成 `../ebook-translator-novel_v<版本号>.zip`，在交付前检查压缩包是否
可安装，并打印出可直接执行的 `calibre-customize -a …` 命令。图形界面下的等价
操作是：*首选项 → 插件 → 从文件加载插件*。

由于两个插件的设置彼此独立，首次使用本分支时需要重新填写各引擎的 API 密钥。

---

## 主要功能

* 支持「小说模式」，通过持续维护的上下文更好地发挥大模型的能力
* 支持「高级模式」和「批量模式」，适用于不同的使用场景
* 支持所选翻译引擎所支持的语言（如 Google 翻译支持 134 种）
* 支持多种翻译引擎，包括 Google 翻译、ChatGPT、Gemini、DeepL、OpenRouter 等
* 支持自定义翻译引擎（支持解析 JSON 和 XML 格式响应）
* 支持所有 Calibre 所支持的电子书格式（输入 48 种，输出 20 种）以及 .srt 等额外格式
* 支持批量翻译电子书，每本书的翻译过程同时进行互不影响
* 支持缓存翻译内容，在请求失败或网络中断后无需重新翻译
* 提供大量自定义设置，如将翻译的电子书存到 Calibre 书库或指定位置

---

## 用户手册

除上文所述的分支专属设置外，上游文档同样适用于本分支。

* [安装插件](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/wiki/简体中文#安装插件)
* [使用方法](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/wiki/简体中文#使用方法)
* [设置说明](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/wiki/简体中文#设置说明)

---

## 相关链接

* [本分支](https://github.com/itotm/calibre-plugin-ebook-translator)
* [上游项目](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin)
* [上游主页](https://translator.bookfere.com)
* [MobileRead](https://www.mobileread.com/forums/showthread.php?t=353052)
* [详细介绍](https://bookfere.com/post/1057.html)
* [贡献代码](CONTRIBUTING.md)
* [捐助上游作者](https://bookfere.com/donate)

---

## 许可证

[GNU General Public License v3.0](LICENSE)

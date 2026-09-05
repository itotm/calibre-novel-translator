[English](README.md) · __简体中文__

---

# 电子书翻译器（Novel）—— Calibre 插件

![Ebook Translator Calibre Plugin](images/logo.png)

一个可以将电子书翻译成指定语言（原文译文对照）的 Calibre 插件。

> **本仓库是一个分支版本**，源自
> [bookfere/Ebook-Translator-Calibre-Plugin](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin)。
> 插件本身是上游作者的成果，功劳归其所有；本仓库只增加了下列内容。它以独立的
> 插件名称、配置文件和缓存目录注册，因此可以与官方插件**并存安装**，两者互不
> 读写对方的数据。

![Translation illustration](images/sample-sc.png)

---

## 本分支新增的内容

### 小说模式（Novel Mode）

在「高级模式」和「批量模式」之外新增的第三种翻译模式，专为长篇叙事内容设计。
它逐章翻译而非逐段翻译，并让模型始终了解全书已经发生的内容。

该模式来自 [BiG86（Simone Norcini）](https://github.com/BiG86) 提交的
[PR #590](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin/pull/590)，
该 PR 在上游尚未合并，本分支在其基础上作了扩展。

**工作方式**

* 每翻译完一章就重建**滚动摘要**与**动态术语表**（人物、地点、物品），并带入
  后续的每一次请求，从而在全书范围内保持人名、代词、语体和术语一致。
* 分块采用**双重上限**——token 预算与段落数量，任一达到即结束当前块，因此段落
  很短时（对白、目录列表）模型也不会丢失对齐标记。**滑动重叠**会把上一块末尾
  的若干段译文作为上下文重放，并注明无需重译。
* 引擎支持时使用**结构化 JSON 输出**，段落对齐由服务端强制保证，而不再依赖
  提示词的自觉；不支持时回退到文本标记方式。
* 章节边界可取自目录一级、目录二级（适用于一级条目为整本书的合集），或每个
  XHTML 文件算一章；并可按字数过滤封面、书名页等前置内容。
* 独立的对话框显示章节列表及每章状态，并提供摘要、术语表和日志三个标签页。
* 译文随产随写入翻译缓存，因此中断后可**续译**：按章续译，章内还可按段落续译，
  已完成的分块不会被重复付费。最终成书直接由缓存生成，不再调用大模型。

**开销控制**

以下各项都是带有合理默认值的设置，位于 *首选项 → 引擎 → Novel Mode*。

* 一章的摘要与术语表**合并为一次请求**，而不是两次——两者读的都是刚刚译完的
  同一章；最后一章以及篇幅很短的前后置内容则完全跳过，因为其结果无人会用到。
* 提示词只携带**本章确实提到的术语条目**，并设有每次请求的条数上限。否则不断
  增长的术语表会在每次请求上消耗输入 token，还会诱使模型把「无需重复」的名单
  当作新条目抄回来。
* 摘要与术语表调用**关闭推理**（两者都不是推理任务），其回复长度设有硬上限，
  过长的摘要在写入前会被截断——否则它会在此后每一章的提示词中被反复读取。
* 分块大小以**模型实际能写出的长度**为上限。可读与可写是两回事：上下文窗口动辄
  数十万 token，而回复上限最低只有 4096，模型写不完的分块会中途截断并被重新
  索取。该数值取自供应商的模型列表，并显示在设置中模型的下方。
* 请求中固定不变的部分排在前面，以便供应商的**提示词缓存**命中；对 Claude 还会
  显式标记缓存断点。
* 启动翻译不再第二次转换整本电子书，译文段落按分块一次性写入缓存。

### OpenRouter 引擎

[OpenRouter](https://openrouter.ai) 通过一个兼容 OpenAI 的接口代理数百个模型。
本引擎继承自 ChatGPT 引擎，只增加该网关特有的部分。所有设置的默认值都是中性的，
即**不写入请求**——OpenRouter 对未提供的参数不会代为填充默认值。

| 分类 | 设置项 |
|---|---|
| 推理 | `effort`（默认 `none`）、`max_tokens` 预算、`exclude` |
| 采样 ¹ | `top_k`、`min_p`、`top_a` |
| 惩罚 ¹ | `frequency_penalty`、`presence_penalty`、`repetition_penalty` |
| 上限 | `max_tokens`、`seed` |
| 供应商路由 | `only`、`order`、`ignore`、`quantizations`、`sort`、`data_collection`、`allow_fallbacks`、`require_parameters`、`zdr` |
| 归属标识 | `HTTP-Referer`、`X-Title` |
| 兜底入口 | 以 JSON 形式追加的请求头与请求体字段 |

¹ 收在「高级参数」复选框之后：这些是专业性很强的旋钮，整本书的翻译用不到。

兜底入口在最后合并进请求，因此界面未暴露的内容（`logit_bias`、`stop`、
`transforms`、`plugins` 等）仍可发送；JSON 格式有误时会在设置窗口中提示，并在
发送请求时忽略，而不会中断翻译。

模型列表还会给出每个模型的上下文窗口、能写出的最长回复、是否支持 JSON schema，
以及它接受哪些参数。前三项显示在设置中模型的下方，回复上限是小说模式据以确定
分块大小的依据，最后一项则决定请求中实际携带哪些参数。

### 引擎设置

* **推理强度改为可配置项**，不再写死，适用于 ChatGPT、Azure ChatGPT、DeepSeek
  以及任何自定义 OpenAI 兼容接口。保持「默认」会完全不发送该字段，这正是普通
  OpenAI 模型所期待的；`none` 用于抑制某些本地服务未经请求就产生的推理 token
  （例如通过 Ollama 运行的 Gemma）；其余档位则是有意花费推理 token。
* **OpenRouter 采用较低的 temperature**（0.3）：翻译是高确定性任务，多样性在这里
  只是噪声——在六档温度上的实测显示质量随温度上升而下降；较低的取值同时也让模型
  在供应商不强制 JSON schema 时更能守住要求的格式。原有的各个引擎则保持上游自带的
  默认值不变。
* **只发送模型接受的参数**：OpenRouter 目录中有五分之一的模型根本不接受
  `temperature`，而模型无法接受的参数轻则被忽略，重则在开启 `require_parameters`
  时导致请求找不到可用的供应商。同一份列表还决定了是否可以要求 JSON schema。列表
  中未提及的参数仍可通过「额外请求体」强制发送。
* **提示词回归自然语言**：小说模式的提示词字段不再强制任何占位符，模板中没有安置
  的内容会以各自的标签追加在末尾，多余的花括号也不再报错。围绕提示词的框架文本
  被排除在翻译目录之外，因此翻译界面语言不会导致模型收到「一种语言的指令包着另一
  种语言的提示词」。

### 本分支修复的问题

* 结构化输出路径在请求体中强制 `stream: true`，却没有同步告知引擎，导致关闭
  流式响应的引擎会把原始 SSE 数据当作 JSON 解析。
* OpenRouter 会把部分上游供应商的故障放在 HTTP 200 的响应体里；现在会给出真正的
  错误信息，而不是含糊的解析失败。
* 小说模式分支遗留的测试预期已与代码对齐。

---

## 与官方插件并存安装

本分支以 **Ebook Translator (Novel)** 的身份注册，并保存自己的状态，因此与官方
插件的安装之间不共享任何内容：

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

它会生成 `../ebook-translator-novel_v<版本号>.zip`，在交付前检查压缩包是否可
安装，并打印出可直接执行的 `calibre-customize -a …` 命令。图形界面下的等价操作
是：*首选项 → 插件 → 从文件加载插件*。

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

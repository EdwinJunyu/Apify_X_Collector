---
version: alpha
name: ACG Observatory
description: 中文桌面采集管理台，以来源、审核和运行状态为工作中心。
colors:
  primary: "#176B68"
  background: "#EFF3F6"
  surface: "#FFFFFF"
  ink: "#192E40"
  muted: "#536677"
  border: "#CBD6DE"
  danger: "#AD343E"
  focus: "#176B68"
typography:
  sans:
    fontFamily: "Microsoft YaHei UI, Noto Sans CJK SC, sans-serif"
  display:
    fontFamily: "Bahnschrift, Microsoft YaHei UI, sans-serif"
  mono:
    fontFamily: "Cascadia Mono, monospace"
rounded:
  DEFAULT: "6px"
spacing:
  control: "36px"
  page: "24px"
components:
  button: {height: "36px"}
  table: {height: "38px"}
  dialog: {width: "540px"}
---

# ACG Observatory

## Overview

面向 Edwin 的 Windows 本地数据积累与审核。桌面产品界面，不是宣传网页。视觉参考是档案工作台：左侧深蓝目录、右侧白色工作面，顶部一条来源与运行状态带；海绿只用于当前操作和导航标记。避免游戏式装饰和无关动漫图片。
界面中文，作品原文保留日文；面向中国大陆/英文 App 的开发者，不是日本市场产品。使用普通公历；记录 UTC，计划时区 America/Vancouver。
Token 规范值在本文件；desktop/theme.py 为 Qt 样式适配器，测试检查值一致。当前仅浅色主题，不依赖动画表达状态。

## Colors

primary 表示操作与焦点；danger 只用于删除与错误。状态均附文字，不仅靠颜色。背景与 surface 区分主工作面，ink 用于正文，muted 用于次级说明。

## Typography

中文系统字体为正文，Bahnschrift 为英文品牌和标题，等宽字体仅用于 ID。实际环境按 Qt 可用字体回退。原文使用 PlainText，不解析富文本。

## Layout

默认 1320×860，最小 960×640。侧栏 210px，页面间距 24px，表格横向滚动，表单纵向滚动。窄桌面仍保留同一导航与动作位置；不是移动端界面。

## Elevation & Depth

通过底色、边框与留白分层，无卡片阴影。模态对话框由 Qt 管理。

## Shapes

6px 轻圆角；表格紧凑行高 38px。按钮最小高度 36px，焦点边框清晰。

## Components

共享 DataPage 负责表格、单行选择、排序、筛选、分页、空状态。EditorDialog 管理所有表单；异步失败保留值并聚焦错误区。Native QComboBox/QDateTimeEdit 对应平台键盘行为；StatusBar 为统一反馈。删除需输入目标 ID 确认；退出 UI 与停止 Worker 分开。没有自动播放、自动缩略图或远端图片加载。

## Do's and Don'ts

- 保留错误文本和重试入口，失败不得显示成功。
- 采集、预算、权限统一由 Worker 校验。
- 不使用网页风格 hero、营销文案或无作用的统计卡片。
- 不把待 M6 的备份/调度按钮伪装成可用功能。

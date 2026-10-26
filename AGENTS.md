# AgenticSRE Research Instructions

## 本项目协作约定

- 使用中文说明进度和结果，代码与标识符保持原样。
- 编译、测试、构建和部署均在远程服务器或集群执行，不在本机安装依赖或运行应用构建。
- 每轮修改完成并通过相应验证后，自动提交并推送到 GitHub 与 GitCode 的主分支；这是用户持续授权的默认流程，无需逐次询问。
- 提交应包含本轮实际完成的改动，保留其他工作和两个仓库各自的提交历史，不使用强制推送。
- 提交和推送不等于生产部署；部署需按用户授权的范围单独执行。
- 不在文件、日志或提交中写入密钥、密码或访问令牌。

This workspace contains the AgenticSRE implementation and the research report
`report/多智能体协作与演化技术调研报告.md`.

When updating the report:

- Treat technical reviewers as the primary audience.
- Prefer decision-ready analysis over outline-style bullets.
- Keep claims traceable to cited papers, project artifacts, or reproducible
  local experiments.
- Distinguish published papers, arXiv preprints, industry reports, and internal
  project materials.
- Do not cite a downloaded file as a PDF paper until `pdfinfo` or `pdftotext`
  confirms it is a valid PDF rather than an HTML landing page.
- Regenerate the PDF with `python3 report/export_pdf.py` after substantive
  report edits.
- Use `doc/downloaded_papers/` for collected papers and generated text extracts.


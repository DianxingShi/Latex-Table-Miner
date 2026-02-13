<div align="center">

# Latex Table Miner

[English](README.md) | [简体中文](README_CN.md)

</div>
<div align="center"><img src="assets/LatexTableMiner.png" /></div> 
**Latex Table Miner** is a powerful tool for extracting and managing LaTeX tables, designed to help researchers and developers efficiently extract, organize, and reuse tables from academic papers.

![GitHub stars](https://img.shields.io/github/stars/DianxingShi/LatexTableMiner?style=social)

## 📦 Features

This tool provides the following core functionalities:

- **arXiv Extraction**: Extract table source code directly from arXiv paper links.
- **Local Extraction**: Support extracting tables from local LaTeX files.
- **Management System**: A convenient interface to view and organize your extraction history.
- **Notes & Remarks**: Add custom notes to extracted tables for easy retrieval later.
- **Source & Dependency Copying**: One-click copying of the table's LaTeX source code and necessary package dependencies.

## ⚙️ How It Works (Core Principles)

We use a multi-stage process to ensure accurate extraction:

1.  **LLM Extraction**: Utilize Large Language Models (LLM) to initially identify and extract potential table fragments.
2.  **Regex Positioning**: Use regular expressions to precisely locate the table within the source code based on context.
3.  **Regex Fix**: Apply predefined regex rules to fix common formatting errors.
4.  **LLM Fix**: For complex compilation errors, call the LLM again for intelligent repair.

## ⚠️ Notes & Known Issues (Todo List)

Due to the diversity and complexity of LaTeX tables, please note the following:

- [ ] **Success Rate**: LaTeX table structures can be intricate and environment dependencies complex, so **100% extraction success is not guaranteed**.
- [ ] **Rendering Display**: Some large tables may not display completely in the preview interface (cropped), but this usually **does not affect the correctness of the source code**. Please try copying the source code to your LaTeX editor (Wait, e.g., Overleaf) to compile and view the complete table.
- [ ] **Processing Speed**: The extraction process may be slow, especially if the initial extraction fails to compile and triggers AutoFix or LLM Fix. Please check the **progress bar** at the bottom of the interface and be patient.

## 🔧 Setup (Important)

Please download `tectonic.exe` and place it in the same directory as `main.py`.

[Download Tectonic v0.15.0](https://github.com/tectonic-typesetting/tectonic/releases/tag/tectonic%400.15.0)

## 📝 User Requirements

To better use this tool, users should have some **basic knowledge of LaTeX**.

*   To ensure compilation universality, we include many common dependencies in the extracted source code by default.
*   When copying for use, please exercise judgment and discern which parts are essential components to avoid unnecessary package conflicts.

## 🤝 Contribution & Feedback

- **Issues**: If you encounter any problems, please submit an Issue.
- **Pull Requests**: Experts are welcome to submit Branches to help optimize this project.

## 🚫 Copyright Notice

- **Strictly No Commercial Use**: This project is for learning and academic exchange only. **Commercial use or reselling is strictly prohibited**.
- **Author Link**: The software contains hyperlinks pointing to my GitHub homepage. Feel free to follow!

## 🌟 Star Trend

If this tool helps you, please give me a **Star** ⭐! It encourages me a lot.

## Star History

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=DianxingShi/Latex-Table-Miner&type=date&legend=top-left)](https://www.star-history.com/#DianxingShi/Latex-Table-Miner&type=date&legend=top-left)

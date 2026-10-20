# 分类表

脚本 `scripts/tidy.py` 里的 `CATEGORY` 就是这个表。改完脚本要重跑 dry-run。

| 目标文件夹 | 扩展名 |
|---|---|
| 图片 | png jpg jpeg gif webp bmp svg ico heic raw psd |
| 文档 | pdf doc docx xls xlsx ppt pptx txt md csv epub rtf odt |
| 视频 | mp4 mkv avi mov wmv flv webm m4v |
| 音频 | mp3 wav flac aac ogg m4a ape |
| 压缩包 | zip rar 7z tar gz bz2 xz iso |
| 安装包 | exe msi apk dmg pkg deb rpm |
| 代码 | py js ts java c cpp h cs go rs sh bat ps1 json yaml yml toml html css xml sql |
| 其他 | 以上都不匹配的，一律进「其他」 |

## 永远跳过

- `.lnk` 快捷方式（挪了会失效观感差，也容易让人找不到）
- `.tmp` `.part` `.crdownload`（还在下载中，挪了会下载失败）
- `desktop.ini` `Thumbs.db` `.DS_Store` 和所有 `.` 开头的隐藏文件
- 没有扩展名的文件

## 想更细怎么办

比如把「文档」拆成「文档/表格」「文档/演示」，就在 `EXT_INDEX` 生成之前把
`.xlsx` 单独塞进新分类即可。别改太多，分类越细，用户越找不到东西。

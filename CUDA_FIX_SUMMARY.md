# CUDA DLL 加載問題修復總結

## 問題根本原因
`cublas64_12.dll` 和其他 CUDA 運行庫在 Windows 上找不到，導致 faster_whisper 導入失敗。

根本問題：`asr.py` 在模塊級別嘗試導入 `faster_whisper`（第12-14行），但 CUDA DLL 路徑在那時還沒有註冊。`_prepare_windows_cuda_dll_paths()` 函數只在 worker 線程中調用，為時已晚。

## 修改內容

### 1. [asr.py](asr.py) - 核心修復
- 將 `_prepare_windows_cuda_dll_paths()` 函數移至模塊頂部（第9-37行）
- 在所有導入之前立即調用它（第39行）
- 移除重複的函數定義

**效果**：現在當 asr.py 被導入時，DLL 路徑會立即被設置，然後 faster_whisper 導入才會嘗試

### 2. [.github/workflows/ci.yml](.github/workflows/ci.yml) - PR/CI 防護
- 添加 CUDA DLL 加載煙霧測試
- 在便攜式環境驗證步驟中添加：
  ```powershell
  & $portablePython -c "import asr; from faster_whisper import WhisperModel; print('[SUCCESS]...')"
  ```
- 這會在 PR 和 push 時檢測到任何 CUDA 相關回歸

### 3. [.github/workflows/release.yml](.github/workflows/release.yml) - 發佈防護
- 添加發佈前驗證步驟
- 在發佈前運行相同的 CUDA DLL 加載測試
- 如果失敗，整個發佈流程會中止

## 驗證結果

✅ 本地測試通過：
```
✅ asr.py 導入成功
✅ faster_whisper 導入成功
✅ gui_app.py 導入成功
```

## 架構層面的雙重保護

1. **應用級**：asr.py 模塊頂部註冊 DLL 路徑
2. **構建級**：build_portable.ps1 的 sitecustomize.py 作為備份層
3. **CI/CD 級**：自動化測試防止未來回歸

## 剩餘假設

- 假設 PyTorch CUDA 在目標系統上可用（或退回到 CPU）
- 假設 sitecustomize.py 在便攜式構建中被正確應用
- 假設 nvidia-* pip 包在 site-packages/nvidia/ 正確安裝

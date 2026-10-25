# GitHub公開の手順

公開候補作成スクリプトは、Git操作やアップロードを行いません。元プロジェクトに既存の`.git`があっても変更しません。

1. `PUBLIC_CANDIDATE_DIRECTORY`に表示されたディレクトリを確認する。
2. `PUBLIC_MANIFEST.json`と`evidence/evaluation_attempts.json`を確認し、ソースや資料を目視する。
3. GitHubで空の公開リポジトリを作成する。名称案は`pai-final-project-daichira`。すでに同名リポジトリがある場合は、その用途を確認し、無条件に上書きしない。
4. Gitの送信者情報は、個人名・実メールが公開コミットへ入らないよう、利用アカウントの設定を確認する。必要ならGitHubのnoreplyメールを使用する。
5. 新しい公開候補の中でのみ、以下を実行する。URLの`YOUR_GITHUB_USERNAME`を自分のGitHubユーザー名へ置き換える。

```bash
cd /実際のPUBLIC_CANDIDATE_DIRECTORY
# 既存の.gitがない新規公開候補を対象にする。
git init -b main
git add .
git diff --cached --stat
# 差分とコミット用の名前・メールを確認してから進む。
git commit -m 'Add Physical AI final project and evaluation evidence'
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/pai-final-project-daichira.git
git push -u origin main
```

認証はGitHubの通常のログイン方法を使い、トークンをURL・コマンド・チャットへ貼らない。`push --force`は使わない。

README内の動画未掲載の記述は、実際のYouTube URLを得てから更新する。公開後は、リポジトリとPDFをログアウト状態で開いて確認する。

元の`checkpoints/`ZIPや元プロジェクト全体を、そのまま公開リポジトリへ入れない。公開候補ZIPにも自動検出できない秘密・個人情報がないかを確認する。

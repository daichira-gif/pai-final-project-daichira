# 日本語の指示を検証済み行動計画に変換する移動ロボット

Physical AI 応用1 最終課題。OmniCampusアカウント：`daichira`。

日本語 → OpenRouter LLM → JSON → 入力検証 → ドライラン → 人による確認 → ROS 2 → Gazebo内のTurtleBot3、という構成です。
LLMの学習は行わず、推論APIを利用します。速度制御と停止は決定論的なPythonコードで実装しています。

## 確認したこと

- 「10cm前進 → 左30度 → 10cm前進」をLLM生成計画から実行し、全ステップと停止コマンドの成功を確認。
- 距離ログ0.034mで自動SIGINTを送信し、後続ステップを抑止。終了コード130、ノードのゼロ速度後処理、プロセスグループ回収を確認。
- 正常4種類・拒否4種類で、再試行を含め期待結果を確認。対応する記録11試行の内訳は成功8・API障害3。初回一括8/8成功ではありません。
- これは既知の開発用ケースで、未知の指示の一般精度や実機の安全性を示すものではありません。

公開候補作成時に、実際の結果ファイルを照合した集計を `evidence/evaluation_attempts.json` に保存します。元のAPI応答・生ログは公開候補に含めません。

## 環境

既存Windows/Docker/WSL2環境、Ubuntu内のPython 3.10系、ROS 2 Humble、Gazebo Classic 11.10.2、TurtleBot3 Burgerで検証しました。
ROS 2、colcon、TurtleBot3 Gazebo関連パッケージが必要です。Linuxのプロセスグループ機能を使用するため、WindowsネイティブPythonでの実行は対象外です。
TurtleBot3等の既存教材・シミュレータ本体は同梱していません。ROSパッケージの依存は `src/pai_final_control/package.xml` を参照してください。

現在のモデル：`nvidia/nemotron-3.5-super-vl:free`。無料枠・Providerの可用性は保証されません。
プランナー本体のAPI呼出しは標準ライブラリを使用し、OpenAI SDKは不要です。

## ビルド

以下はUbuntuコンテナ内で実行します。保存先を変えた場合はパスを読み替えてください。

```bash
source /opt/ros/humble/setup.bash
mkdir -p /home/ubuntu/pai_final_build
cd /home/ubuntu/pai_final_build
colcon --log-base /home/ubuntu/pai_final_build/log build \
  --base-paths /workspace/PAI_final_project/src \
  --build-base /home/ubuntu/pai_final_build/build \
  --install-base /home/ubuntu/pai_final_build/install \
  --symlink-install --packages-select pai_final_control
source /home/ubuntu/pai_final_build/install/setup.bash
ros2 pkg executables pai_final_control
```

新規環境での完全再現は今後の確認事項です。`--log-base` は `build` の前に置きます。

## Gazeboの起動

すでに起動している場合は重複起動しません。

```bash
source /opt/ros/humble/setup.bash
export TURTLEBOT3_MODEL=burger
ros2 launch turtlebot3_gazebo turtlebot3_world.launch.py
```

別端末で制御パッケージのsetupをsourceし、`ros2 topic info /cmd_vel --verbose` と `ros2 topic info /odom --verbose` で接続を確認します。

## 走行しない確認

```bash
cd /workspace/PAI_final_project
python3 src/task_executor.py config/plans/demo_move_rotate_move.json
python3 experiments/step23/test_invalid_plans.py
python3 experiments/step23/test_validation_boundaries.py
python3 experiments/step26/test_llm_plan_openrouter.py
```

最初のコマンドは `DRY_RUN_PASS: no robot motion` が期待値です。プランナーのテストはAPIを模擬します。

## LLMによる計画生成

```bash
python3 src/llm_plan_openrouter.py --ask-key \
  '10cm前進して、左に30度回転し、その後10cm前進してください'
```

キーは非表示入力です。録画は入力後に行い、キーをコード・引数・Git・動画に含めません。
生成だけではロボットは動きません。`PLAN_FILE` を確認し、距離・回転方向・順序が元の指示と一致するか、人が確認します。
APIエラーの場合は未成功として記録し、別モデルへ自動切替したり再試行したりしません。

## 動作実行（明示的な別操作）

周囲とシミュレータの状態を確認したうえで、出力された実際の計画パスを使います。

```bash
python3 src/task_executor.py /absolute/path/to/generated/plan.json --execute
```

失敗時は後続ステップを開始せず、停止を試みます。`STOP_COMMAND_OK` はゼロ速度Publisherの正常終了を意味し、物理停止の保証ではありません。
動作後は別端末で確認します。

```bash
ros2 topic echo /odom --field twist.twist
```

数サンプル確認後、Ctrl+Cで表示を終了します。これは計測用echoの終了であり、走行中の割込み試験とは区別します。

## 仕様と限界

- move：0.05〜0.5m／ステップ、rotate：絶対値5〜180度、最大8ステップ。
- 正の角度は左、負は右。速度指定は不可。前進0.04m/s、最大角速度0.30rad/s。
- 距離は開始方向への投影、旋回はyaw差分の累積。残距離1cm／角度誤差1.5度以内で到達とします。
- 曖昧な距離、上限超過、後退、速度指定は、開発用評価では拒否対象です。
- 構造検証は指示意味や経路の安全性を保証しません。
- 障害物回避、地図ナビゲーション、カメラ認識、実機実証は未実装・未実施です。
- API障害が複数回発生しています。可用性、停止遅延・距離の連続測定、独立評価は残る課題です。

## 資料

- [説明PDF](reports/説明資料_PAI最終課題_daichira.pdf)
- [開発過程の詳細HTML](reports/PAI_Final_Project_Report_20261025.html)
- [動画撮影・編集台本](docs/VIDEO_SCRIPT_JA.md)
- [提出チェックリスト](docs/SUBMISSION_CHECKLIST_JA.md)

動画URLは撮影・掲載後に追記します。GitHub、YouTube、OmniCampusの公開・提出完了は、このREADMEの作成をもって完了したことにはなりません。

## 最終回帰試験（2026-10-25）

現行版でオフライン・模擬試験6群82件が合格しました。準備9、プロセス20、不正計画3、境界値12、プランナー30、評価処理8です。記録はevidence/evaluation_attempts.json内のfinal_regressionを参照してください。

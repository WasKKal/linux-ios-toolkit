using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEngine;

/// <summary>
/// 通用 iOS 导出脚本（纯 Linux Unity Editor 可用）。
/// batchmode 调用：
///   Unity -batchmode -quit -projectPath /proj -executeMethod BuildIos.Export
/// 环境变量：
///   IOS_EXPORT_DIR   导出目录（默认 /out/iOSBuild）
///   IOS_BUILD_SCENES 逗号分隔的场景相对路径；未设时自动取 BuildSettings 中已勾选的场景
///   IOS_BUNDLE_ID    bundle identifier（可选，覆盖 PlayerSettings）
/// </summary>
public static class BuildIos
{
    public static void Export()
    {
        string outDir = Environment.GetEnvironmentVariable("IOS_EXPORT_DIR");
        if (string.IsNullOrEmpty(outDir)) outDir = "/out/iOSBuild";

        // 1) 场景列表：环境变量优先，否则用 Build Settings 里 enabled 的场景
        string[] scenes;
        string scenesEnv = Environment.GetEnvironmentVariable("IOS_BUILD_SCENES");
        if (!string.IsNullOrEmpty(scenesEnv))
        {
            scenes = scenesEnv.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries)
                              .Select(s => s.Trim()).ToArray();
        }
        else
        {
            scenes = EditorBuildSettings.scenes
                .Where(s => s.enabled)
                .Select(s => s.path)
                .ToArray();
        }

        if (scenes.Length == 0)
        {
            Debug.LogError("[BuildIos] 无可构建场景：请在 Build Settings 勾选场景，或设 IOS_BUILD_SCENES");
            EditorApplication.Exit(2);
            return;
        }

        string bundleId = Environment.GetEnvironmentVariable("IOS_BUNDLE_ID");
        if (!string.IsNullOrEmpty(bundleId))
            PlayerSettings.SetApplicationIdentifier(BuildTargetGroup.iOS, bundleId);

        // 2) iOS 目标关键设置：IL2CPP + ARM64
        PlayerSettings.SetScriptingBackend(BuildTargetGroup.iOS, ScriptingImplementation.IL2CPP);
        PlayerSettings.SetArchitecture(BuildTargetGroup.iOS, 2); // ARM64 = 2
        PlayerSettings.iOS.targetOSVersionString = "12.0";
#if UNITY_2022_1_OR_NEWER
        PlayerSettings.SetApiCompatibilityLevel(BuildTargetGroup.iOS, ApiCompatibilityLevel.NET_Standard_2_0);
#else
        PlayerSettings.SetApiCompatibilityLevel(BuildTargetGroup.iOS, ApiCompatibilityLevel.NET_4_6);
#endif
        AssetDatabase.SaveAssets();

        // 3) 导出 Xcode 工程（BuildPlayer 不直接出 IPA；后续由 linux_ipa_build.py 出包）
        var options = new BuildPlayerOptions
        {
            scenes = scenes,
            locationPathName = outDir,
            target = BuildTarget.iOS,
            options = BuildOptions.None
        };
        BuildReport report = BuildPipeline.BuildPlayer(options);
        var summary = report.summary;
        Debug.Log($"[BuildIos] result={summary.result} errors={summary.totalErrors} " +
                  $"size={summary.totalSize} time={summary.totalTime}");

        EditorApplication.Exit(summary.result == BuildResult.Succeeded ? 0 : 1);
    }
}

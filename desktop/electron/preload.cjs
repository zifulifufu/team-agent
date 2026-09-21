// 把主进程生成的一次性 token 和后端地址交给渲染进程(仅用于访问本机 Python 后端)。
const { contextBridge, ipcRenderer } = require("electron");

const argOf = (name) => {
  const a = process.argv.find((x) => x.startsWith(`--${name}=`));
  return a ? a.slice(name.length + 3) : "";
};

// 令牌只交给应用自己的页面(打包后的 file:// 页面,或开发时的 localhost:5173),
// 万一窗口被导航到别的网页,那个网页拿不到令牌。
const isAppPage = location.protocol === "file:" || location.origin === "http://localhost:5173";

contextBridge.exposeInMainWorld("teamAgent", {
  token: isAppPage ? argOf("team-agent-token") : "",
  api: isAppPage ? argOf("team-agent-api") : "",
  // 弹出系统的「选择文件夹」对话框,返回路径;取消返回 null
  pickFolder: isAppPage ? () => ipcRenderer.invoke("team-agent:pick-folder") : () => Promise.resolve(null),
});

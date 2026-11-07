import re

with open("app/worker.py", "r", encoding="utf-8") as f:
    text = f.read()

pattern = re.compile(
    r'(\s+# 動態句式引導：整段都在問就硬轉斷言.*?)(\n\s+# 定向對象優先於抽到的情緒：)',
    re.DOTALL
)

m = pattern.search(text)
assert m, "Pattern not found"

new_code = '''        # 句型結構多元性：依隨機抽樣分流（35% 短碎調侃、35% 純色誘直球、30% 生活場景）
        # 徹底打破「[人名] + [我剛在做某事] + [你敢不敢/看你有沒有本事]」的四合一長句固定模子
        struct_roll = random.random()
        if struct_roll < 0.35:
            # 35% 極短碎語／純情緒起鬨（8~16字）：不報備在幹嘛、不扯場景，直截了當
            chosen_syntax = (
                "【句型模式・短碎口語（8~16字）】：\\n"
                "1. 這則請說【極簡短的台灣在地大白話】，嚴禁寫成超過20字的大長句！\\n"
                "2. 絕不要交代「我剛在做什麼」「我在哪裡」等生活前情，也別提工作或飲料！\\n"
                "3. 純粹針對上一句順著起鬨、隨口吐槽或簡單撩一句（如「笑死你很扯」「太虛了吧」「真上場別求饒喔」「這嘴我給滿分」），俐落短句即可！"
            )
        elif struct_roll < 0.70:
            # 35% 純色誘／肉體直球（12~24字）：不扯生活瑣事，直接進身體細節或性張力
            chosen_syntax = (
                "【句型模式・直球色誘（12~24字）】：\\n"
                "1. 這則不需要交代你在哪上班、在喝什麼飲料，把生活瑣事全部拿掉！\\n"
                "2. 直接從【純粹的男女肉體張力、觸感、尺寸或敏感帶】直球切入！\\n"
                "3. 大膽發騷，用大白話直接講你想被怎麼幹、想摸哪裡或調侃他硬不硬，短而辛辣！"
            )
        else:
            # 30% 生活場景／自身隨性狀態（15~30字）：原有生活瑣事心境
            chosen_syntax = (
                "【句型模式・生活日常隨念（15~30字）】：\\n"
                "1. 自然帶出你當下隨性的小動作或周圍環境，不用刻意找人名點名。\\n"
                "2. 語法完整自然隨性，就像真實女生在群裡隨手發一條動態一樣輕鬆。"
            )

        if "不准問" in type_note:
            chosen_syntax += "\\n【句式多元指令（嚴禁連續反問）】：上一句水軍就是問句，這則【結尾一定要落在斷言或動作】，【絕對禁止】以「…嗎？」「…吧？」反問收尾。"
        elif "問句收尾" in type_note:
            chosen_syntax += "\\n【句式指令・把問句要回來】：最近水軍全在下判斷，這則【結尾一定是一個真問題】，讓群友能自然接話。"'''

updated = text[:m.start()] + new_code + text[m.end(1):]
with open("app/worker.py", "w", encoding="utf-8") as f:
    f.write(updated)
print("Patch applied successfully via tools/patch_syntax.py")

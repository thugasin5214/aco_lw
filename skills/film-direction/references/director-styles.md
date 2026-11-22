# Director style lenses (condensed)

Condensed from the `风格参数 / Style parameters` blocks of DirectorSKILL's director_styles modules
(github.com/wuwangzhang1216/DirectorSKILL, MIT, © wangzhang-wu). High-level methods only — never copy
specific shots, lines, characters or plots from real films.

## spielberg — Steven Spielberg 斯皮尔伯格
- lens: Emotional mainstream narrative, family stakes, awe via reaction-before-reveal
- lens kit mm: 21, 35, 50, 135; camera height: 成人对话眼平；奇观与威胁段落降到 1.0-1.3m 的孩子视线高度，机位锁定
- prefer: slow dolly-in to close-up, walking-pace tracking follow, crane down into eye level, motivated pan connecting person to threat
- avoid: dutch angle, unmotivated handheld, orbit around subject, snap zoom
- motion budget: medium; shot sizes: medium close-up reaction, wide with three-plane depth, medium two-shot, insert on the carried object
- composition: three-plane deep staging, silhouette against a bright named source, doorway frame-within-frame
- palette: 琥珀、钨丝暖黄、灰蓝绿 (saturation mid, contrast mid)
- editing: ASL 3-7 动作段；5-9 情绪段 s; transitions hard cut, match cut, dissolve
- AI clip: 4-8 s; prompt bias: 每条 clip 只装一次看、一个摄影机行为、一个环境反应；反应镜头单独成条，先生成反应再生成揭示。

## hitchcock — Alfred Hitchcock 希区柯克
- lens: Suspense via shared knowledge, voyeurism, mistaken identity, controlled withholding
- lens kit mm: 28, 50, 100, 135; camera height: 眼平为主；命运节拍升到 3-6 米俯拍，威胁节拍降到 0.3-0.6 米
- prefer: locked frame held past comfort, slow dolly-in on a detail, point-of-view pan following an eyeline, high overhead on a stairwell or floor plan
- avoid: handheld, camera orbit, whip pan, crash zoom
- motion budget: low; shot sizes: extreme close-up on the object, close-up reaction, point-of-view medium, obstructed medium two-shot
- composition: foreground obstruction 20-35% of frame, frame within frame via door/stair/window, geometric shadow cast across the wall
- palette: 中性冷灰、米白、家具木色 (saturation mid, contrast high)
- editing: ASL 4-9 基线；0.4-1.5 仅在暴力瞬间 s; transitions hard cut, match cut, dissolve
- AI clip: 3-6 用于延迟节拍；1-2 用于碎片化插入 s; prompt bias: 一条 clip 只做一件事——要么是"角色靠近"，要么是"危险移动"，绝不同时。把观众已知的危险放在前景遮挡或画面边缘，让它几乎不动。

## kubrick — Stanley Kubrick 库布里克
- lens: Wide-angle one-point perspective, central symmetry, cold formalism, allegory
- lens kit mm: 18, 25, 35; camera height: 锁定在层高的一半（2.8-3.2m 层高对应 1.4-1.6m），天花与地面的收拢楔形才上下对称；或 0.5-0.8m 低位跟随。一场戏内高度不变
- prefer: locked symmetrical frame, constant-speed dolly-in along the vanishing axis, constant-speed dolly-out on the same axis, low tracking follow at 0.5-0.8m
- avoid: handheld, camera orbit, whip pan, any acceleration or deceleration in a move
- motion budget: low; shot sizes: symmetrical wide with strong vanishing point, frontal medium on the centre axis, overhead insert of floor pattern or object, long-held frontal close-up
- composition: one-point perspective, vanishing point at frame centre, bilateral symmetry or exact mirroring, subject riding the centre axis
- palette: 冷白、灰、纯黑 (saturation mid, contrast mid)
- editing: ASL 12-40，极少低于 6 s; transitions hard cut, match cut
- AI clip: 8-12（推轨需要行程；短于 6 秒看不出匀速）；12-40 秒的长凝视用同参数分段续接拼出来 s; prompt bias: 把"匀速"和"不重新构图"写成硬约束；一条 clip 只有一个直线运动，且运动方向必须与消失点轴线重合。

## kurosawa — Akira Kurosawa 黑泽明
- lens: Epic ensemble, humanism, weather as drama, dynamic action space
- lens kit mm: 50, 100, 250; camera height: 眼平或略低；泥地段落降到 0.6-0.9m 让泥水进入前景
- prefer: lateral tracking across a formation, long-lens locked hold on the waiting group, pan sweeping the line, single decisive push down the depth axis
- avoid: camera orbit, handheld shake as default, crash zoom, slow-motion hero move
- motion budget: medium; shot sizes: extreme wide with figures under 1/6 frame height, long-lens compressed group band, close-up on a wet exhausted face, insert of feet in mud
- composition: lateral bands stacked by long-lens compression, triangular or wedge formation pointing at the conflict, high ground versus low ground as class information
- palette: 土黄、泥褐、湿布墨黑、雨天灰白 (saturation mid, contrast high)
- editing: ASL 8-20 等待段；2-5 战斗段 s; transitions hard cut, wipe, match cut
- AI clip: 3-6 战斗；6-10 等待 s; prompt bias: 先写天气对身体的作用（湿透的布、被吹动的头发、泥中的脚），再写动作；镜头运动只写"横移"或"锁定"，不要写复合运动。

## scorsese — Martin Scorsese 斯科塞斯
- lens: Urban energy, guilt, voice-over, kinetic camera, moral fall
- lens kit mm: 18, 27, 40, 75; camera height: 眼平；入场长镜头维持 1.5-1.6m 恒定，暴力节拍可掉到腰高
- prefer: steadicam long take entering a space, dolly-in that accelerates then stops hard, snap zoom on a face, lateral tracking along a bar or table
- avoid: unmotivated handheld shake, drifting float with no endpoint, slow drone-like flyover, symmetrical locked wide
- motion budget: high; shot sizes: steadicam medium tracking, close-up on excitement or shame, wide of the room as a social map, insert of money/hands/glass
- composition: subject centred with the world streaming past behind, layered background of people who all have business, practical light source burning in frame
- palette: 酒吧红、后厨绿、钠黄街灯、包厢金 (saturation high, contrast high)
- editing: ASL 1.5-3 音乐蒙太奇；8-15 崩塌段（无音乐） s; transitions hard cut on the downbeat, jump cut, freeze frame
- AI clip: 2-4 蒙太奇碎片；8-12 入场长镜头 s; prompt bias: 把摄影机运动写成有终点的句子（"tracks with him until he stops at the bar, then holds"），不要写"camera moves through the scene"。速度曲线必须显式：slow start, faster middle, hard stop。

## fellini — Federico Fellini 费里尼
- lens: Dream logic, circus parade, autobiographical fantasy, grotesque tenderness
- lens kit mm: 18, 25, 32; camera height: 眼平游动为主；垂直事件时升到 2.5-4m 让人群变成图案
- prefer: drifting track through a crowd at 0.4-0.8 m/s, pan handing off from one face to the next, frontal wide close-up with mild distortion, crane rising over a procession
- avoid: locked symmetrical wide, long-lens compression, shallow-focus isolation, static shot-reverse-shot
- motion budget: high; shot sizes: wide of the whole procession, frontal close-up of a single strange face, medium tracking through bodies, over-shoulder past a foreground passer-by
- composition: figures entering continuously from both frame edges, foreground passers-by occupying 10-30%, reframed every 2-3 seconds, never settled
- palette: 粉、金、酒红、天蓝 (saturation high, contrast mid)
- editing: ASL 6-14；结尾段拉到 15-30 s; transitions hard cut on a musical phrase end, match cut on a recurring image, dissolve
- AI clip: 6-12 s; prompt bias: 先写人群的行为清单（谁在做什么），再写摄影机；把"持续有人从画面边缘进出"写成硬要求，把音乐节拍写进动作描述（walking on the beat of a waltz）。

## bergman — Ingmar Bergman 伯格曼
- lens: Interior psychological drama, face close-ups, existential silence
- lens kit mm: 75, 100, 150; camera height: 眼平，精确对齐演员瞳孔高度；全场不变
- prefer: locked frontal close-up on the eyeline axis, two-face composition with one profile in foreground, focus pull between two faces instead of a cut, imperceptible push-in at 0.02-0.05 m/s
- avoid: handheld, shot-reverse-shot on fixed A/B setups, camera orbit, wide establishing of the room
- motion budget: low; shot sizes: extreme close-up filling 60-80% of frame height, two-face composition, medium close-up at 75mm, hands or object insert
- composition: camera on the eyeline axis, deviation under 10 degrees, two faces overlapping at their edges, one frontal one profile, near-black or near-white flat background
- palette: 灰蓝、灰绿、近黑、近白 (saturation low, contrast high)
- editing: ASL 20-90；一场 4 分钟的戏只用 5-8 个镜头 s; transitions hard cut, fade
- AI clip: 8-15（在工具能稳住身份的前提下取最长） s; prompt bias: 一条 clip 只写一次微表情变化 + 一次呼吸事件，摄影机写成 locked 或 imperceptible push-in；显式写 the whole face stays in focus。唯一例外是双脸变焦点那一条——它要从 negative 里拿掉 shallow depth of field，改写成 focus shifts from the front face to the face behind over 1.5 seconds。20 秒以上的长镜头用同参数分段续接拼出来，接点选在动作完全静止的那一帧。

## tarkovsky — Andrei Tarkovsky 塔可夫斯基
- lens: Poetic time, long takes, water and fire, spiritual search
- lens kit mm: 28, 35, 50; camera height: waist to chest, level or slightly low, planted like a witness
- prefer: locked hold, very slow lateral dolly, slow push-in on an object, slow tilt down to water
- avoid: handheld shake, whip pan, orbit, crash zoom
- motion budget: low; shot sizes: wide, medium wide, insert or detail, over-shoulder from behind
- composition: deep staging with three or more planes, reflective foreground, off-center or back-turned figure
- palette: mud brown, wet stone grey, moss green, sepia (saturation low, contrast low)
- editing: ASL 30-90 s; transitions hard cut, slow dissolve, fade
- AI clip: 8-15 s; prompt bias: 主体动作写到最小（呼吸、转头、放下一件东西），描述预算全部给环境运动和材质：水、火、雾、布料、光的缓慢变化。

## wong_kar_wai — Wong Kar-wai 王家卫
- lens: Urban loneliness, missed connections, neon, fragmented memory, voice-over
- lens kit mm: 18, 25, 85; camera height: chest to shoulder, often from a corner or across an obstruction, rarely level with the eyes
- prefer: small-amplitude handheld, step-print slow motion, reframe-in-shot, shoot through foreground obstruction
- avoid: locked tripod, symmetric center framing, smooth gimbal glide, drone
- motion budget: medium; shot sizes: medium close-up, close-up of hands and objects, obstructed medium, over-shoulder
- composition: foreground obstruction in every frame, subject pushed to frame edge, frame within frame via door or window
- palette: neon green, deep red, tungsten amber (saturation high, contrast high)
- editing: ASL 1.5-4 s; transitions hard cut, jump cut, whip pan
- AI clip: 4-8 s; prompt bias: 先写前景挡什么，再写霓虹的两种颜色，最后才写人做什么；动作只留一个，且必须是'收回'而不是'伸出'。

## nolan — Christopher Nolan 诺兰
- lens: High-concept structure, time puzzles, large-format practical spectacle
- lens kit mm: 14, 21, 35, 50; camera height: eye level, camera close to the body, occasionally low to give machinery mass
- prefer: controlled handheld follow, wide tracking alongside action, slow push-in on a mechanism, crane up to reveal scale
- avoid: orbit for its own sake, whip pan, slow motion for beauty, dreamy diffusion
- motion budget: high; shot sizes: medium close-up, wide tracking, extreme wide for scale, mechanism insert
- composition: figure against oversized real structure, horizon low to give sky weight, mechanism readable in frame
- palette: concrete grey, steel blue, black, canvas khaki (saturation low, contrast mid)
- editing: ASL 2-5 s; transitions hard cut, match cut, cut on sound
- AI clip: 3-6 s; prompt bias: 一条 clip 只讲一个机制事实：一个动作、一个镜头运动、一个可见的计时器变化。把'尺度'翻译成具体材料与尺寸，不要写'宏大'。

## villeneuve — Denis Villeneuve 维伦纽瓦
- lens: Monumental scale, mist, geometric symmetry, low-rumble silence
- lens kit mm: 21, 40, 75; camera height: eye level or extreme overhead; nothing in between
- prefer: locked wide, constant-speed lateral track, constant-speed push toward vanishing point, high overhead
- avoid: handheld, orbit, whip pan, speed ramp
- motion budget: low; shot sizes: extreme wide, wide, overhead, medium close-up
- composition: center symmetry, single strong horizon at one-eighth or seven-eighths height, human at 5-15 percent of frame height
- palette: one hue per sequence: ochre sand, teal fog, copper interior, ink green, grey-white ice (saturation low, contrast mid)
- editing: ASL 8-18 s; transitions hard cut, fade, dissolve
- AI clip: 8-15 s; prompt bias: 先写几何体与材料，再写雾从第几米开始，最后才写人；人的描述不超过一句，且必须给出他占画面高度的比例。

## fincher — David Fincher 芬奇
- lens: Surgical control, teal-amber palette, procedural investigation, restrained dread
- lens kit mm: 35, 85, 135; camera height: exact eye level or 10cm above, repeatable to the centimetre
- prefer: locked precise frame, imperceptible 3-6 percent push-in, slow pull-back on a mechanical dolly, surveillance framing from behind glass
- avoid: handheld, camera shake, orbit, whip pan
- motion budget: low; shot sizes: medium close-up, close-up, medium from a surveillance position, insert on hands and documents
- composition: subject isolated by shallow depth at f/2-f/2.8, background compressed into unreadable color blocks, framing through glass or a door gap
- palette: teal-green shadow, desaturated charcoal, cold grey (saturation low, contrast low)
- editing: ASL 4-9 s; transitions hard cut, cut to black
- AI clip: 4-8 s; prompt bias: 点名唯一实用光源实体，把推进写成百分比，把景深写成光圈值；主体动作限定为一个职业性小动作（对齐、擦拭、翻页、点击）。

## refn — Nicolas Winding Refn 雷弗恩
- lens: Neon color blocks, symmetric ritual framing, slow gaze, sudden violence
- lens kit mm: 25, 40, 75; camera height: chest height, perpendicular to the wall, subject's centre line exactly on frame centre
- prefer: locked frontal symmetric frame, single constant push under 5cm per second, locked face portrait held 12-25s, overhead centred on the ritual
- avoid: handheld, orbit, pan, tilt
- motion budget: none; shot sizes: medium frontal, close-up frontal portrait, wide frontal of the room as a stage, overhead
- composition: absolute bilateral symmetry, flat frontal plane with no depth cues, subject dead centre
- palette: magenta, electric blue (saturation high, contrast high)
- editing: ASL 10-25 s; transitions hard cut
- AI clip: 8-15 s; prompt bias: 先写颜色分界线在画面的哪个位置，再写人物姿势，最后只给一个微动作；运动预算给光，不给摄影机也不给人。

## bi_gan — Bi Gan 毕赣
- lens: Misty SW-China small towns, single ultra-long take, dream-memory loops, poetic voice-over
- lens kit mm: 18, 25, 35; camera height: shoulder to chest, with a slight breathing rise and fall, never perfectly level
- prefer: continuous gimbal drift at walking pace, follow from one to two seconds behind, pass-through a door or bridge or tunnel as a transition, drift away from the subject at the end
- avoid: cut, locked tripod, absolute drone smoothness, whip pan
- motion budget: medium; shot sizes: medium following shot, wide traversal, over-shoulder from behind, medium close-up in passing
- composition: wet reflective foreground, doorway and tunnel framing as transition gates, mixed light sources within one frame
- palette: humid teal-green, dim purple, tungsten amber (saturation low, contrast low)
- editing: ASL 45-600 s; transitions pass-through as transition, fade, dissolve
- AI clip: 10-20 s; prompt bias: 先写路径（起点、途经点、终点）和每个点的光源类型，再写摄影机的漂移速度与滞后量，人物动作只写走路与一次停顿。

## zhang_yimou — Zhang Yimou 张艺谋
- lens: Saturated single-color fields, mass choreography as image, folk-ritual iconography
- lens kit mm: 35, 50, 85, 135; camera height: eye level 1.6m locked, or 90-degree overhead; 中间高度不用
- prefer: locked symmetrical wide, constant-speed lateral track parallel to a rank, slow push-in on the one figure breaking the pattern, rise from eye level to 90-degree overhead
- avoid: handheld shake, orbit, whip pan, dutch angle
- motion budget: medium; shot sizes: extreme wide with the color field, wide symmetrical, overhead pattern shot, medium close-up of the single break
- composition: single-hue field filling 60-80% of frame, grid of repeating units with one break, one-point perspective through a courtyard or gallery
- palette: one hue per movement — red, ochre yellow, jade green, or ink blue (saturation high, contrast mid)
- editing: ASL 4-10 仪式段；3-6 冲突段 s; transitions hard cut, match cut on pattern, dissolve
- AI clip: 3-6 s; prompt bias: 先写颜色和承载它的材料，再写队列的行列数与统一动作，最后写那唯一一个不同步的人；每条 clip 只做一个恒速运动。

## hou_hsiao_hsien — Hou Hsiao-hsien 侯孝贤
- lens: Static distant long take, doorway depth, real-time duration, ellipsis over exposition
- lens kit mm: 50, 85, 100; camera height: seated eye height 1.0-1.2m, locked, unchanged for the whole scene
- prefer: locked long hold on sticks, minimal reframe pan under 20 degrees, hold on the empty room after everyone leaves, shoot one room from inside another through a doorway
- avoid: push-in, handheld, crane, orbit
- motion budget: none; shot sizes: wide, medium wide, doorway-framed wide, medium two-shot at distance
- composition: doorway or window frame eating the outer quarter of the frame, three simultaneous planes of activity, figures small and off-centre
- palette: aged wood brown, whitewash grey, foliage green outside the door, washed indigo cloth (saturation mid, contrast mid)
- editing: ASL 40-120 s; transitions hard cut, fade to black
- AI clip: 8-12 s; prompt bias: 把整个房间写成一张同时进行的小动作清单（前中后各一件），然后明确要求摄影机什么都不做。

## park_chan_wook — Park Chan-wook 朴赞郁
- lens: Baroque revenge, improbable camera geometry, ironic beauty over cruelty, pattern density
- lens kit mm: 28, 40, 50, 85; camera height: chest height 1.4m frontal and locked; the flourish changes height mid-move
- prefer: one impossible continuous move per scene (through a wall, floor, or 90-degree rotation), locked frontal symmetrical hold, constant-speed lateral track along a patterned wall, 90-degree overhead of the act
- avoid: handheld verite, documentary reframe, unmotivated dutch angle, random shake
- motion budget: high; shot sizes: frontal symmetrical medium, wide showing the whole decorated room, ornamental insert, 90-degree overhead
- composition: at least three distinct patterns visible per interior frame, frontal symmetry with one deliberate off-axis element, frame within frame through a decorated doorway
- palette: burgundy, olive green, mustard gold, walnut brown (saturation high, contrast mid)
- editing: ASL 3-8，含一次 40-90 秒的形式长镜 s; transitions hard cut, match cut on pattern or shape, sudden 0.5s ornamental insert
- AI clip: 4-8 s; prompt bias: 点名画面里三种具体图案，然后把摄影机运动写成起点—路径—终点三段式的确定路径（下降、后退、旋转各写出量）；人物只做一个礼节性动作。

## malick — Terrence Malick 泰伦斯·马力克
- lens: Available light, wide wandering handheld at magic hour, whispered voice-over, associative montage
- lens kit mm: 14, 21, 28, 35; camera height: continuously variable 0.3-1.8m, never settles
- prefer: drifting handheld just behind a shoulder at walking pace, low sweep up past a body toward sky or canopy, slow arc while walking around a figure, sudden tilt away from a face to a hand, grass, or water
- avoid: locked tripod hold, symmetrical frontal composition, zoom, whip pan
- motion budget: high; shot sizes: close insert on hands and objects, medium close following just off a shoulder, wide with a figure small against sky, low angle through grass or foliage
- composition: huge foreground element with the horizon behind it, sun in frame behind the subject, figure entering or leaving frame constantly
- palette: grass green, wheat gold, sky blue-white, skin (saturation mid, contrast low)
- editing: ASL 2-5，每 60-90 秒给一颗 6-10 秒不切的呼吸镜头 s; transitions hard cut, dissolve on light, cut away mid-gesture
- AI clip: 3-5 s; prompt bias: 一条 clip 只装一个手势、一个持续漂移的运动、一个在动的自然元素；永远不装情节动作。

## michael_mann — Michael Mann 迈克尔·曼
- lens: Urban night as a cold luminous field, professional competence, loneliness as architecture
- lens kit mm: 18, 25, 35, 50; camera height: eye level 1.6m, lens axis level; dash or hood height inside vehicles
- prefer: locked wide with the horizon at eye level, loose handheld from a moving vehicle, constant-speed lateral track along a glass wall, static hold on a figure 30-60m away
- avoid: crane reveal, orbit, dutch angle, slow motion
- motion budget: medium; shot sizes: wide with the standing figure 8-15% of frame height, medium two-shot at 2-4m separation, procedural insert on hands and equipment, static long-lens hold at distance
- composition: horizon or skyline at eye level even in interiors, figure small against a bright lit field, glass reflection layering city over face
- palette: sodium amber, mercury blue-green, LED cyan-white, black (saturation mid, contrast high)
- editing: ASL 3-8 s; transitions hard cut
- AI clip: 3-6 s; prompt bias: 点名每一个市政光源及其颜色和方位，给室内也写上窗外的天际线，让主体小而背景亮。

## coen_brothers — Coen Brothers 科恩兄弟
- lens: Deadpan geometric dark comedy, wide low-mounted moves, fate as a flat joke
- lens kit mm: 14, 21, 27, 40; camera height: 0.4-0.6m tilted up 10-15 degrees for the running moves; dead-frontal eye level 1.6m, axis level, for the deadpan holds
- prefer: low-mounted wide dolly running ahead of or behind a walking subject, dead-frontal locked symmetrical hold, fast push-in on an object, 90-degree overhead of a mess being cleaned up
- avoid: drifting handheld, orbit, shallow-focus beauty shot, slow motion
- motion budget: medium; shot sizes: dead-frontal medium, wide with subject centred in a flat landscape or wall, low-angle wide with ceiling and floor both in frame, extreme close-up as punctuation
- composition: subject dead centre with a door, window or sign behind the head like a halo, wide-angle with both ceiling and floor visible, the object of value at exact frame centre
- palette: institutional beige, motel brown, snow white, fluorescent green-white (saturation mid, contrast mid)
- editing: ASL 4-9，每颗对话镜头的出点后拖 0.5-1.5 秒 s; transitions hard cut, hard cut across a violent size jump (wide to extreme close-up)
- AI clip: 3-6 s; prompt bias: 先建一个正面对称的死中构图，给主体一个过度认真的小动作，然后让摄影机要么完全不动，要么走一条直线。

# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
import curses

def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(1)
    stdscr.timeout(100)

    sh, sw = stdscr.getmaxyx()
    snake = [(0, 0), (0, 1), (0, 2)]
    direction = curses.KEY_RIGHT

    while True:
        stdscr.clear()

        next_head = (snake[0][0], snake[0][1])
        if direction == curses.KEY_DOWN:
            next_head = (snake[0][0] + 1, snake[0][1])
        if direction == curses.KEY_UP:
            next_head = (snake[0][0] - 1, snake[0][1])
        if direction == curses.KEY_LEFT:
            next_head = (snake[0][0], snake[0][1] - 1)
        if direction == curses.KEY_RIGHT:
            next_head = (snake[0][0], snake[0][1] + 1)

        snake.insert(0, next_head)
        stdscr.addch(next_head[0], next_head[1], '*')

        if snake[0] == food:
            food = None
        else:
            tail = snake.pop()
            stdscr.addch(tail[0], tail[1], ' ')

        if (not 0 <= snake[0][0] < sh or
            not 0 <= snake[0][1] < sw or
            snake[0] in snake[1:]):
            curses.endwin()
            quit()

        if stdscr.getch() == ord('q'):
            break

curses.wrapper(main)

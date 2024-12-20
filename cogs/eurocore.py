import discord
import requests
import os

from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Modal, View
from typing import Optional, Dict, Literal

from components.bot import Bot
from components.user import User

class Job:
    id: int
    user: User
    action: Literal["add", "edit", "remove"]
    created_at: datetime
    modified_at: datetime
    status: Literal["queued", "success", "failure"]
    dispatch_id: Optional[int]
    error: Optional[str]
    ping_on_completion: bool
    message: Optional[discord.Message]

    def __init__(
            self,
            user: User,
            job_id: int,
            action: Literal["add", "edit", "remove"],
            created_at: datetime,
            modified_at: datetime,
            status: Literal["queued", "success", "failure"],
            ping_on_completion: bool,
            dispatch_id: int | None = None,
            error: str | None = None,
    ):
        self.id = job_id
        self.user = user
        self.action = action
        self.created_at = created_at
        self.modified_at = modified_at
        self.status = status
        self.dispatch_id = dispatch_id
        self.error = error
        self.ping_on_completion = ping_on_completion
        self.message = None

    def __repr__(self):
        return f"Job(id={self.id}, status={self.status})"

    def set_message(self, message: discord.Message):
        self.message = message

    def embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"Job {self.id}: {self.status.title()}", color=discord.Color.blurple())
        embed.add_field(name="Job ID", value=self.id, inline=True)
        embed.add_field(name="Action", value=self.action, inline=True)
        embed.add_field(name="Status", value=self.status, inline=True)
        embed.add_field(name="", value="", inline=False)
        embed.add_field(name="Job Created", value=f"<t:{int(self.created_at.timestamp())}>", inline=True)
        embed.add_field(name="Job Modified", value=f"<t:{int(self.modified_at.timestamp())}:R>", inline=True)
        if self.dispatch_id and self.action != "remove":
            embed.add_field(name="View Dispatch", value=f"https://www.nationstates.net/page=dispatch/id={self.dispatch_id}", inline=False)
        embed.add_field(name="Error", value=f"```{self.error}```", inline=False)
        embed.set_footer(text=f"Initiated by {self.user.name}")

        return embed

    async def update(self, bot: Bot):
        async with bot.client.get(url=f"{bot.config.eurocore_url}/queue/dispatch/{self.id}") as response:
            response_data = await response.json(encoding="UTF-8")

            self.status = response_data["status"]
            self.modified_at = datetime.strptime(response_data["modified_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            self.dispatch_id = response_data["dispatch_id"]
            self.error = response_data["error"]

            await self.message.edit(embed=self.embed())

class RegistrationModal(Modal, title="register for eurocore"):
    def __init__(self, bot: Bot):
        super().__init__()

        self.bot = bot

    username = discord.ui.TextInput(
        label="username",
        min_length=3,
        max_length=20,
        required=True
    )

    password = discord.ui.TextInput(
        label="password",
        min_length=8,
        max_length=40,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        username = self.username.value.strip()
        password = self.password.value.strip()

        user = await self.bot.register(interaction.user.id, username, password)

        self.bot.logger.info(f"registered user: {user.name}")

        await interaction.response.send_message(f"registration successful, welcome, {user.name}!", ephemeral=True) # noqa

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        self.bot.logger.error(f"registration error ({type(error)}): {error}")

        await interaction.response.send_message(f"registration failed: {error}, please try again", ephemeral=True) # noqa


class LoginModal(Modal, title="login to eurocore"):
    def __init__(self, bot: Bot):
        super().__init__()

        self.bot = bot

    username = discord.ui.TextInput(
        label="username",
        min_length=3,
        max_length=20,
        required=True
    )

    password = discord.ui.TextInput(
        label="password",
        min_length=8,
        max_length=40,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        username = self.username.value.strip()
        password = self.password.value.strip()

        user = User(interaction.user.id, username, password)

        await self.bot.sign_in(user)

        self.bot.user_list.add_user(interaction.user.id, user)

        self.bot.logger.info(f"logged in user: {user.name}")

        await interaction.response.send_message(f"login successful, welcome back, {user.name}!", ephemeral=True) # noqa

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        self.bot.logger.error(f"login error ({type(error)}): {error}")

        await interaction.response.send_message(f"login failed: {error}, please try again", ephemeral=True) # noqa


class Eurocore(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot

        self.jobs: Dict[int, Job] = {}

    def cog_load(self):
        self.bot.logger.info("loading eurocore, starting jobs task")
        self.poll_jobs.start()

    def cog_unload(self):
        self.bot.logger.info("unloading eurocore, stopping jobs task")
        self.poll_jobs.stop()

    @tasks.loop(seconds=10)
    async def poll_jobs(self):
        self.bot.logger.debug("polling jobs")

        for message_id, job in self.jobs.items():
            await job.update(self.bot)

            if job.status != "queued":
                if job.ping_on_completion:
                    await job.message.reply(f"<@!{job.user.id}>")

        self.jobs = {message_id: job for message_id, job in self.jobs.items() if job.status == "queued"}

    @poll_jobs.before_loop
    async def before_poll_jobs(self):
        await self.bot.wait_until_ready()

    @poll_jobs.error
    async def on_poll_jobs_error(self, error):
        self.bot.logger.error(f"polling jobs error: {error}")

    async def get_user(self, interaction: discord.Interaction) -> User:
        if interaction.user.id not in self.bot.user_list:
            modal = LoginModal(self.bot)
            await interaction.response.send_modal(modal) # noqa
            await modal.wait()

        user = self.bot.user_list[interaction.user.id]

        if datetime.now() - user.last_login > timedelta(hours=12):
            await self.bot.sign_in(user)

        return user

    async def dispatch(self, interaction: discord.Interaction, method: str, data: Optional[dict] = None, dispatch_id: Optional[int] = None, ping: bool = False):
        user = await self.get_user(interaction)

        headers = {
            "Authorization": f"Bearer {user.token}"
        }

        async with self.bot.client.request(
                method,
                url=f"{self.bot.config.eurocore_url}/dispatch{f'/{dispatch_id}' if dispatch_id else ''}",
                headers=headers,
                json=data
        ) as response:
            response_data = await response.json(encoding="UTF-8")

            job = Job(
                job_id=response_data["id"],
                user=user,
                action=response_data["action"],
                created_at=datetime.strptime(response_data["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc),
                modified_at=datetime.strptime(response_data["modified_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc),
                status=response_data["status"],
                ping_on_completion=ping,
                dispatch_id=response_data["dispatch_id"],
                error=response_data["error"],
            )

            if interaction.response.is_done(): # noqa
                message = await interaction.followup.send(embed=job.embed())
            else:
                await interaction.response.send_message(embed=job.embed()) # noqa
                message = await interaction.original_response()

            job.set_message(message)

            self.jobs[job.id] = job

    @app_commands.command(name="register", description="register for eurocore")
    async def register(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RegistrationModal(self.bot)) # noqa

    @app_commands.command(name="login", description="login to eurocore")
    async def login(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LoginModal(self.bot)) # noqa

    dispatch_command_group = app_commands.Group(name="dispatch", description="eurocore dispatch commands")

    @dispatch_command_group.command(name="add", description="post a dispatch")
    @app_commands.choices(nation=[
        app_commands.Choice(
            name=val.replace("_", " ").title(), value=val) for val in requests.options(
            f"{os.getenv("EUROCORE_URL")}/dispatch"
            # the program will exit if this variable isn't defined during the `config` init
        ).headers["X-Nations"].split(",")
    ])
    @app_commands.choices(category=[
        app_commands.Choice(name="Bulletin: Policy", value=305),
        app_commands.Choice(name="Bulletin: News", value=315),
        app_commands.Choice(name="Bulletin: Opinion", value=325),
        app_commands.Choice(name="Bulletin: Campaign", value=385),

        app_commands.Choice(name="Meta: Gameplay", value=835),
        app_commands.Choice(name="Meta: Reference", value=845),
    ])
    @app_commands.describe(
        title="dispatch title",
        nation="eurocore nation",
        category="NS dispatch category",
        content=".txt file containing the dispatch text",
        ping="receive a ping when the job is completed",
    )
    async def add_dispatch(
            self,
            interaction: discord.Interaction,
            title: str,
            nation: app_commands.Choice[str],
            category: app_commands.Choice[int],
            content: discord.Attachment,
            ping: bool = False,
    ):
        if not content.content_type == "text/plain; charset=utf-8":
            # TODO: make this a custom error
            raise commands.UserInputError("content must be a .txt file")

        text = (await content.read()).decode("UTF-8")

        data = {
            "title": title,
            "nation": nation.value,
            "category": int(str(category.value)[:1]),
            "subcategory": category.value,
            "text": text
        }

        await self.dispatch(interaction, "POST", data, ping=ping)

    @dispatch_command_group.command(name="edit", description="edit a dispatch")
    @app_commands.choices(category=[
        app_commands.Choice(name="Bulletin: Policy", value=305),
        app_commands.Choice(name="Bulletin: News", value=315),
        app_commands.Choice(name="Bulletin: Opinion", value=325),
        app_commands.Choice(name="Bulletin: Campaign", value=385),

        app_commands.Choice(name="Meta: Gameplay", value=835),
        app_commands.Choice(name="Meta: Reference", value=845),
    ])
    @app_commands.describe(
        dispatch_id="NS dispatch id",
        title="dispatch title",
        category="NS dispatch category",
        content=".txt file containing the dispatch text",
        ping="receive a ping when the job is completed",
    )
    @app_commands.rename(dispatch_id="id")
    async def edit_dispatch(
            self,
            interaction: discord.Interaction,
            dispatch_id: int,
            title: str,
            category: app_commands.Choice[int],
            content: discord.Attachment,
            ping: bool = False,
    ):
        if not content.content_type == "text/plain; charset=utf-8":
            raise commands.UserInputError("content must be a .txt file")

        text = (await content.read()).decode("UTF-8")

        data = {
            "title": title,
            "category": int(str(category.value)[:1]),
            "subcategory": category.value,
            "text": text
        }

        await self.dispatch(interaction, "PUT", data, dispatch_id, ping)

    @dispatch_command_group.command(name="delete", description="delete a dispatch")
    @app_commands.describe(dispatch_id="NS dispatch id", ping="receive a ping when the job is completed")
    @app_commands.rename(dispatch_id="id")
    async def delete_dispatch(self, interaction: discord.Interaction, dispatch_id: int, ping: bool = False):
        await self.dispatch(interaction, "DELETE", dispatch_id=dispatch_id, ping=ping)

async def setup(bot: Bot):
    await bot.add_cog(Eurocore(bot))
